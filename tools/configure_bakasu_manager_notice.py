#!/usr/bin/env python3
"""Hide BakaSU's author-signature notice only for the trusted paired build.

The upstream official-author test stays byte-for-byte intact. A separate
repository-build flag requires the installed package, actual APK signer and
the running kernel's manager recognition to agree before hiding that notice.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import uuid

PACKAGE = 'com.resukisu.resukisu'
MARKER = 'GKI_REPOSITORY_MANAGER_TRUST_V1'
JAVA = 'manager/app/src/main/java/org/bakasu/bakasu/'
FILES = {
    'cli': JAVA + 'data/shell/KsuCli.kt',
    'model': JAVA + 'domain/model/KernelState.kt',
    'repository': JAVA + 'data/kernel/KernelRepository.kt',
    'home': JAVA + 'ui/screen/main/HomePage.kt',
}
OFFICIAL_METHOD = '''    suspend fun isOfficialSignature(packageResourcePath: String): Boolean = withContext(Dispatchers.IO) {
        val shell = getRootShell()
        val out = shell.newJob()
            .add("${getKsuDaemonPath()} debug get-sign ${shellQuote(packageResourcePath)}")
            .to(ArrayList<String>(), null).exec().out
        out.firstOrNull()?.trim()
            .orEmpty() == "size: 0x377, hash: d3469712b6214462764a1d8d3e5cbe1d6819a0b629791b9f4101867821f1df64"
    }
'''
OFFICIAL_STATUS = '''            isOfficialSignature = runCatching {
                ksuCliRepository.isOfficialSignature(application.packageResourcePath)
            }.getOrDefault(false),
'''


class NoticeError(ValueError):
    """A changed source layout or invalid public identity must not be patched."""


def validate_identity(document: dict) -> dict:
    fields = {'manager_package', 'manager_cert_size', 'manager_cert_sha256'}
    if (not isinstance(document, dict) or set(document) not in (fields, fields | {'schema_version'})
            or document.get('schema_version', 1) != 1
            or ('schema_version' in document and type(document['schema_version']) is not int)
            or document.get('manager_package') != PACKAGE
            or type(document.get('manager_cert_size')) is not int
            or not 64 <= document['manager_cert_size'] <= 4096
            or not isinstance(document.get('manager_cert_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', document['manager_cert_sha256'])):
        raise NoticeError('Expected the BakaSU public package and exact signing certificate identity')
    return {field: document[field] for field in fields}


def read_identity(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise NoticeError('Public manager certificate JSON is missing or invalid')
    def unique_object(items):
        document = {}
        for key, value in items:
            if key in document:
                raise NoticeError('Public manager identity contains a duplicate field')
            document[key] = value
        return document
    try:
        document = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_object)
    except (ValueError, UnicodeError) as error:
        raise NoticeError('Public manager certificate JSON is invalid') from error
    return validate_identity(document)


def replacements(identity: dict) -> dict[str, tuple[str, str]]:
    identity = validate_identity(identity)
    signature = f"size: {identity['manager_cert_size']:#x}, hash: {identity['manager_cert_sha256']}"
    method = f'''
    // {MARKER}
    suspend fun isRepositorySignature(
        packageName: String,
        packageResourcePath: String,
    ): Boolean = withContext(Dispatchers.IO) {{
        if (packageName != "{PACKAGE}") return@withContext false
        val result = getRootShell().newJob()
            .add("${{getKsuDaemonPath()}} debug get-sign ${{shellQuote(packageResourcePath)}}")
            .to(ArrayList<String>(), null).exec()
        if (!result.isSuccess || result.out.size != 1) return@withContext false
        result.out.single().trim() == "{signature}"
    }}
'''
    model = '    val isOfficialSignature: Boolean = true,\n'
    repository = f'''            // {MARKER}
            isTrustedRepositoryBuild = isManager && runCatching {{
                ksuCliRepository.isRepositorySignature(
                    application.packageName,
                    application.packageResourcePath,
                )
            }}.getOrDefault(false),
'''
    home = '                if (!uiState.systemStatus.isOfficialSignature) {\n'
    new_home = f'''                // {MARKER}
                if (
                    !uiState.systemStatus.isOfficialSignature &&
                    !uiState.systemStatus.isTrustedRepositoryBuild
                ) {{
'''
    return {
        'cli': (OFFICIAL_METHOD, OFFICIAL_METHOD + method),
        'model': (model, model + f'    val isTrustedRepositoryBuild: Boolean = false, // {MARKER}\n'),
        'repository': (OFFICIAL_STATUS, OFFICIAL_STATUS + repository),
        'home': (home, new_home),
    }


def read_source(root: Path, relative: str) -> tuple[Path, bytes, str, str]:
    path = root / relative
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise NoticeError('BakaSU notice sources must not contain linked paths')
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise NoticeError('Expected the original BakaSU notice source layout')
    raw = path.read_bytes()
    try:
        text = raw.decode('utf-8')
    except UnicodeError as error:
        raise NoticeError('BakaSU notice source is not UTF-8') from error
    newline = '\r\n' if '\r\n' in text else '\n'
    text = text.replace('\r\n', '\n')
    if '\r' in text or (newline == '\r\n' and raw.count(b'\n') != raw.count(b'\r\n')):
        raise NoticeError('BakaSU notice source has unsupported mixed newlines')
    return path, raw, text, newline


def configure_notice(source: Path, identity: dict) -> list[str]:
    identity = validate_identity(identity)
    if source.is_symlink() or not source.is_dir():
        raise NoticeError('Expected an independent BakaSU source directory')
    root = source.resolve(strict=True)
    _, _, kbuild, _ = read_source(root, 'kernel/Kbuild')
    if not re.search(r'(?m)^REPO_NAME\s*:=\s*(?:BakaSU|ReSukiSU)\s*$', kbuild):
        raise NoticeError('Repository notice configuration is limited to native BakaSU sources')
    inputs = {key: read_source(root, relative) for key, relative in FILES.items()}
    markers = [text.count(MARKER) for _, _, text, _ in inputs.values()]
    if markers not in ([0] * len(FILES), [1] * len(FILES)):
        raise NoticeError('BakaSU notice sources contain a partial or duplicate repository patch')
    patched = bool(markers[0])
    changes = []
    for key, (before, after) in replacements(identity).items():
        path, original, text, newline = inputs[key]
        if patched:
            if text.count(after) != 1:
                raise NoticeError('BakaSU notice patch or prepared signer changed; review before rebuilding')
            # Reconstruct the original layout and re-run its exact-anchor check.
            text = text.replace(after, before, 1)
        if text.count(before) != 1:
            raise NoticeError('BakaSU notice source layout changed; review its native warning before rebuilding')
        if key == 'cli' and ('isRepositorySignature' in text or text.count('isOfficialSignature(') != 1):
            raise NoticeError('BakaSU signer methods changed; review before rebuilding')
        if key != 'cli' and 'isTrustedRepositoryBuild' in text:
            raise NoticeError('BakaSU repository trust field already exists outside the expected patch')
        updated = text.replace(before, after, 1).replace('\n', newline).encode('utf-8')
        if updated != original:
            changes.append((path, original, updated))
    # All four source layouts and the complete public identity are validated
    # before any source is touched. Stage siblings and roll back replaced files
    # if a later filesystem replacement fails.
    staged, applied = [], []
    try:
        for path, original, updated in changes:
            temporary = path.with_name('.gki-bakasu-notice-' + uuid.uuid4().hex + '.tmp')
            temporary.write_bytes(updated)
            staged.append((temporary, path, original))
        for temporary, path, original in staged:
            os.replace(temporary, path)
            applied.append((path, original))
    except OSError:
        for path, original in reversed(applied):
            path.write_bytes(original)
        raise
    finally:
        for temporary, _, _ in staged:
            temporary.unlink(missing_ok=True)
    return [str(path.relative_to(root)).replace('\\', '/') for path, _, _ in changes]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--identity', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        changed = configure_notice(args.source, read_identity(args.identity))
    except (OSError, ValueError, UnicodeError) as error:
        print('BakaSU paired-manager notice configuration failed: ' + str(error), file=sys.stderr)
        return 1
    print('BakaSU paired-manager notice configured; original official-author and kernel signature checks retained'
          + (' (already configured)' if not changed else ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

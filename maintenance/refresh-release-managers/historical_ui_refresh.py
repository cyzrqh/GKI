#!/usr/bin/env python3
"""Recompile historical manager UI while preserving the released native payload.

This maintenance driver is intentionally separate from the normal build tools.
It never runs a native builder, signs an APK, or changes a release attachment.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile


UI_COMMIT = '0f61220ea4da784e1633069271103e1687524916'
PROFILES = {
    'SukiSU': ('SukiSU-Ultra/SukiSU-Ultra', 'com.sukisu.ultra'),
    'BakaSU': ('Baka-SU/BakaSU', 'com.resukisu.resukisu'),
    'KernelSU-Next': ('KernelSU-Next/KernelSU-Next', 'com.rifsxd.ksunext'),
    'KernelSU': ('tiann/KernelSU', 'me.weishu.kernelsu'),
    'KowSU': ('KOWX712/KernelSU', 'com.kowx712.supermanager'),
    'FinaSU': ('LyraVoid/FinaSU', 'me.yuki.fina'),
}
ABIS = {'arm64-v8a', 'armeabi-v7a', 'x86_64', 'riscv64'}
SHA = re.compile(r'[0-9a-f]{40}\Z')


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def run(arguments, *, cwd=None, env=None, timeout=600, capture=False):
    print('+ ' + ' '.join(str(item) for item in arguments), flush=True)
    result = subprocess.run(list(map(str, arguments)), cwd=cwd, env=env,
                            capture_output=capture, timeout=timeout)
    if result.returncode:
        if capture:
            sys.stderr.buffer.write(result.stderr)
        raise RuntimeError('Maintenance command failed with status ' + str(result.returncode))
    return result.stdout if capture else None


def git(repository: Path, *arguments) -> str:
    return run(['git', '-C', repository, *arguments], capture=True).decode('utf-8').strip()


def regular(path: Path) -> Path:
    if (path.is_symlink() or not path.is_file()
            or any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in path.parents)):
        raise ValueError('Expected a regular, unlinked input file: ' + str(path))
    return path.resolve(strict=True)


def zip_inventory(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    entries = {}
    if len(archive.infolist()) > 100000:
        raise ValueError('APK has too many entries')
    for item in archive.infolist():
        name = item.filename
        path = PurePosixPath(name)
        mode = item.external_attr >> 16
        if (name in entries or '\\' in name or path.is_absolute() or '..' in path.parts
                or stat.S_ISLNK(mode) or item.file_size > 512 * 1024 * 1024):
            raise ValueError('Invalid or repeated APK entry: ' + name)
        entries[name] = item
    if archive.testzip() is not None:
        raise ValueError('APK ZIP integrity check failed')
    return entries


def original_identity(apk: Path, expected_sha256: str, ui_root: Path) -> dict:
    sys.path.insert(0, str(ui_root / 'tools'))
    from apk_manifest import manifest_identity
    raw = regular(apk).read_bytes()
    if not re.fullmatch(r'[0-9a-f]{64}', expected_sha256) or sha256(raw) != expected_sha256:
        raise ValueError('Original APK does not match the release inventory SHA-256')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = zip_inventory(archive)
        identity = manifest_identity(archive.read('AndroidManifest.xml'))
        metadata = json.loads(archive.read('assets/gki-build.json'))
        variant = metadata.get('ksu_variant')
        if variant not in PROFILES:
            raise ValueError('Unknown original manager identity')
        if (identity['package'] != PROFILES[variant][1]
                or metadata.get('package') != identity['package']
                or metadata.get('ksu_version_code') != identity['version_code']
                or metadata.get('ksu_version_name') != identity['version_name']
                or metadata.get('file') != apk.name
                or metadata.get('producer_repository') != 'cyzrqh/GKI'
                or not SHA.fullmatch(metadata.get('producer_head_sha', ''))
                or not SHA.fullmatch(metadata.get('ksu_ref', ''))
                or not re.fullmatch(r'[0-9a-f]{64}', metadata.get('manager_cert_sha256', ''))
                or type(metadata.get('manager_cert_size')) is not int
                or not 1 <= metadata['manager_cert_size'] <= 16384):
            raise ValueError('Original APK source, package, version or public certificate metadata disagree')
        abis = sorted({name.split('/')[1] for name in names if name.startswith('lib/') and name.count('/') >= 2})
        if 'arm64-v8a' not in abis or not set(abis) <= ABIS:
            raise ValueError('Unknown original native ABI inventory')
        for abi in abis:
            for library in ('libksud.so', 'libkernelsu.so'):
                name = 'lib/' + abi + '/' + library
                if name not in names or names[name].file_size < 20:
                    raise ValueError('Original APK lacks its own daemon/JNI library: ' + name)
        preserved = {}
        for name, item in names.items():
            if item.is_dir():
                continue
            data = archive.read(name)
            if preserved_member(name, data):
                preserved[name] = sha256(data)
    return {'apk': str(apk.resolve()), 'apk_sha256': expected_sha256,
            'manifest': identity, 'metadata': metadata, 'abis': abis, 'preserved': preserved}


def preserved_member(name: str, data: bytes) -> bool:
    # Preserve all assets, including FinaSU's embedded engine packages and the
    # historical gki-build metadata (the final signer writes truthful new metadata).
    return (name.startswith(('lib/', 'assets/')) or name.endswith(('.ko', '.kpm'))
            or data.startswith(b'\x7fELF'))


def export_recipe(repository: Path, commit: str, destination: Path):
    if destination.exists():
        raise ValueError('Historical recipe output already exists')
    actual = git(repository, 'rev-parse', '--verify', commit + '^{commit}')
    if actual != commit:
        raise ValueError('Historical producer commit could not be verified')
    data = run(['git', '-C', repository, 'archive', '--format=tar', commit], capture=True)
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(destination, filter='data')
    (destination / 'producer-commit.txt').write_text(commit + '\n', encoding='ascii')


def python_tool(recipe: Path, name: str, *arguments, env=None):
    tool = regular(recipe / 'tools' / name)
    run([sys.executable, tool, *arguments], cwd=recipe, env=env)


def source_hashes(source: Path) -> dict[str, str]:
    result = {}
    for path in source.rglob('*'):
        name = path.relative_to(source).as_posix()
        if name.startswith('.git/') or name == '.git' or not path.is_file():
            continue
        if path.is_symlink():
            # Upstream contains intentional relative symlinks. Record the link,
            # never hash/read a possible target outside the source checkout.
            result[name] = 'symlink:' + os.readlink(path)
        else:
            result[name] = sha256(path.read_bytes())
    return result


def restore_source(source: Path, identity: dict, recipe: Path, origin: str | None):
    metadata = identity['metadata']
    if source.exists():
        raise ValueError('Manager source directory already exists; refusing to reuse a partially patched tree')
    upstream = origin or 'https://github.com/' + PROFILES[metadata['ksu_variant']][0] + '.git'
    run(['git', 'clone', '--no-checkout', '--no-hardlinks', '--config', 'core.autocrlf=false', upstream, source], timeout=900)
    reference = metadata['ksu_ref']
    try:
        git(source, 'cat-file', '-e', reference + '^{commit}')
    except RuntimeError:
        run(['git', '-C', source, 'fetch', '--no-recurse-submodules', '--tags', 'origin', reference], timeout=600)
    git(source, 'checkout', '--detach', reference)
    if git(source, 'rev-parse', 'HEAD') != reference or git(source, 'rev-parse', '--is-shallow-repository') != 'false':
        raise ValueError('Historical manager checkout is not the exact full-history source')
    git(source, 'fsck', '--connectivity-only', '--no-dangling')
    variant = metadata['ksu_variant']
    # Historical port guards bind the checkout to its public repository, even
    # when an exact local mirror supplied the already verified objects.
    git(source, 'remote', 'set-url', 'origin', 'https://github.com/' + PROFILES[variant][0] + '.git')
    environment = {**os.environ, 'KSU_VARIANT': variant, 'KSU_PACKAGE_NAME': identity['manifest']['package']}
    # Replay the OLD source adapter. The current backend/port is never imported.
    if variant == 'BakaSU':
        python_tool(recipe, 'restore_bakasu_kpm.py', '--source', source, '--reference', reference, env=environment)
    elif variant not in ('SukiSU', 'BakaSU'):
        python_tool(recipe, 'additional_manager_kpm.py', '--source', source, '--reference', reference,
                    '--variant', variant, env=environment)
    builder = recipe / 'tools' / ('build_sukisu_manager.sh' if variant == 'SukiSU'
        else 'build_bakasu_manager.sh' if variant == 'BakaSU' else 'build_additional_manager.sh')
    text = builder.read_text('utf-8')
    if 'manager_ui_defaults.py' in text:
        python_tool(recipe, 'manager_ui_defaults.py', '--source', source, '--variant', variant, env=environment)
    if 'prepare_manager_icons.py' in text:
        python_tool(recipe, 'prepare_manager_icons.py', '--source', source, env=environment)
    if variant == 'BakaSU':
        workflow = recipe / '.github/workflows/kernel-full-feature-matrix.yml'
        if workflow.is_file() and 'configure_bakasu_manager_notice.py' in workflow.read_text('utf-8'):
            public = recipe / 'historical-manager-public-certificate.json'
            public.write_text(json.dumps({'manager_package': identity['manifest']['package'],
                'manager_cert_size': metadata['manager_cert_size'],
                'manager_cert_sha256': metadata['manager_cert_sha256']}) + '\n', encoding='utf-8')
            python_tool(recipe, 'configure_bakasu_manager_notice.py', '--source', source, '--identity', public,
                        env=environment)
        if 'configure_bakasu_kpm_ui.py' in text:
            python_tool(recipe, 'configure_bakasu_kpm_ui.py', '--source', source, env=environment)


def overlay_ui(source: Path, identity: dict, ui_root: Path):
    variant, reference = identity['metadata']['ksu_variant'], identity['metadata']['ksu_ref']
    before = source_hashes(source)
    if variant == 'SukiSU':
        python_tool(ui_root, 'configure_sukisu_kpm_ui.py', '--source', source)
    elif variant == 'BakaSU':
        python_tool(ui_root, 'configure_bakasu_kpm_ui.py', '--source', source)
    else:
        # Run with an isolated interpreter so historical modules cannot shadow
        # the latest UI renderer. manager_ui_plan reads the same original HEAD.
        program = '''from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from additional_manager_kpm_ui import manager_ui_plan
source=Path(sys.argv[2])
for name,data in manager_ui_plan(source,sys.argv[3],sys.argv[4]).items():
    target=source/name
    if target.is_symlink() or any(p.is_symlink() for p in target.parents if p != source):
        raise ValueError('Linked manager UI output')
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(data)
'''
        run([sys.executable, '-c', program, ui_root / 'tools', source, reference, variant])
    after = source_hashes(source)
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    allowed = re.compile(r'manager/app/src/main/(?:java/(?:[A-Za-z0-9_]+/)+ui/.*\.(?:kt|java)|res/values/strings\.xml)\Z')
    if not changed or any(not allowed.fullmatch(name) for name in changed):
        raise ValueError('UI overlay changed an unexpected source path: ' + ', '.join(changed))
    return {name: {'before': before.get(name), 'after': after.get(name)} for name in changed}


def kotlin_mask(text: str) -> str:
    """Keep braces in code; mask Kotlin comments/strings without shifting offsets."""
    mask = list(text)
    index = 0
    while index < len(text):
        start = index
        if text.startswith('//', index):
            end = text.find('\n', index)
            index = len(text) if end < 0 else end
        elif text.startswith('/*', index):
            depth, index = 1, index + 2
            while index < len(text) and depth:
                if text.startswith('/*', index): depth, index = depth + 1, index + 2
                elif text.startswith('*/', index): depth, index = depth - 1, index + 2
                else: index += 1
            if depth: raise ValueError('Unclosed Kotlin comment')
        elif text.startswith('"""', index):
            end = text.find('"""', index + 3)
            if end < 0: raise ValueError('Unclosed Kotlin raw string')
            index = end + 3
        elif text[index] in ('"', "'"):
            quote, index = text[index], index + 1
            while index < len(text):
                if text[index] == '\\': index += 2
                elif text[index] == quote: index += 1; break
                else: index += 1
            else: raise ValueError('Unclosed Kotlin string')
        else:
            index += 1
            continue
        for position in range(start, min(index, len(text))):
            if mask[position] != '\n': mask[position] = ' '
    return ''.join(mask)


def remove_blocks(text: str, name: str) -> tuple[str, int]:
    mask, ranges = kotlin_mask(text), []
    for match in re.finditer(r'\b' + re.escape(name) + r'\s*\{', mask):
        if ranges and match.start() < ranges[-1][1]:
            continue
        depth, end = 1, match.end()
        while end < len(mask) and depth:
            if mask[end] == '{': depth += 1
            elif mask[end] == '}': depth -= 1
            end += 1
        if depth: raise ValueError('Unclosed Gradle ' + name + ' block')
        ranges.append((match.start(), end))
    for start, end in reversed(ranges):
        text = text[:start] + text[end:]
    return text, len(ranges)


GRADLE_GUARD = '''// UI-only maintenance: no new native code is allowed in this task graph.
gradle.taskGraph.whenReady { graph ->
    graph.allTasks.each { task ->
        def name = task.name.toLowerCase(java.util.Locale.ROOT)
        if (task instanceof org.gradle.api.tasks.Exec ||
            (name =~ /.*(externalnativebuild|configurecmake|buildcmake|cargobuild|compilerust|buildrust|ndkbuild|buildsui|buildnomount).*/)) {
            throw new GradleException("UI-only refresh refuses native/Exec task: " + task.path)
        }
    }
}
'''


def prepare_gradle(source: Path, work: Path, identity: dict) -> dict:
    manager = source / 'manager'
    app = manager / 'app/build.gradle.kts'
    root = manager / 'build.gradle.kts'
    app_text, root_text = regular(app).read_text('utf-8'), regular(root).read_text('utf-8')
    plugin = re.compile(r'(?m)^[ \t]*alias\(libs\.plugins\.lsplugin\.apksign\)[ \t]*\n')
    if len(plugin.findall(app_text)) != 1:
        raise ValueError('Review historical APK signing plugin before UI-only build')
    app_text = plugin.sub('', app_text)
    app_text, signing = remove_blocks(app_text, 'apksign')
    if signing != 1 or re.search(r'\b(?:signingConfig|signingConfigs)\b', kotlin_mask(app_text)):
        raise ValueError('Review additional historical signing configuration')
    app_text, native_count = remove_blocks(app_text, 'externalNativeBuild')
    root_text = re.sub(r'(?m)^[ \t]*alias\(libs\.plugins\.lsplugin\.cmaker\)[ \t]*\n', '', root_text)
    root_text, cmaker_count = remove_blocks(root_text, 'cmaker')
    if 'externalNativeBuild' in kotlin_mask(app_text) or re.search(r'\bcmaker\s*\{', kotlin_mask(root_text)):
        raise ValueError('A historical native build hook was not removed completely')
    native_dir = work / 'original-jni'
    native_dir.mkdir()
    with zipfile.ZipFile(identity['apk']) as archive:
        for name in zip_inventory(archive):
            if name.startswith('lib/') and not name.endswith('/'):
                target = native_dir / name.removeprefix('lib/')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
    quote = json.dumps
    manifest = identity['manifest']
    abi_list = ', '.join(quote(abi) for abi in identity['abis'])
    app_text += '''
// Only Java/Kotlin/resources are rebuilt; these values come from the released APK.
android {
    defaultConfig {
        applicationId = %s
        versionCode = %d
        versionName = %s
        ndk { abiFilters.clear(); abiFilters += listOf(%s) }
    }
    buildTypes { getByName("release") { signingConfig = null; ndk.debugSymbolLevel = "NONE" } }
    splits { abi { isEnable = false } }
    sourceSets { getByName("main") { jniLibs.setSrcDirs(listOf(%s)) } }
    packaging { jniLibs { keepDebugSymbols += "**/*.so"; pickFirsts += "**/*.so" } }
}
''' % (quote(manifest['package']), manifest['version_code'], quote(manifest['version_name']),
       abi_list, quote(native_dir.as_posix()))
    app.write_text(app_text, encoding='utf-8', newline='\n')
    root.write_text(root_text, encoding='utf-8', newline='\n')
    guard = work / 'ui-only.gradle'
    guard.write_text(GRADLE_GUARD, encoding='utf-8', newline='\n')
    return {'removed_native_blocks': native_count, 'removed_cmaker_blocks': cmaker_count,
            'guard': str(guard), 'jni_directory': str(native_dir)}


def rebuild_archive(original: Path, compiled: Path, target: Path, identity: dict):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from apk_manifest import manifest_identity
    with zipfile.ZipFile(original) as old, zipfile.ZipFile(compiled) as new:
        old_names, new_names = zip_inventory(old), zip_inventory(new)
        if manifest_identity(new.read('AndroidManifest.xml')) != identity['manifest']:
            raise ValueError('Compiled APK changed the released package/version identity')
        if not any(name.endswith('.dex') for name in new_names):
            raise ValueError('Compiled manager has no executable Kotlin/Java code')
        with zipfile.ZipFile(target, 'x') as output:
            for name, item in new_names.items():
                data = new.read(name)
                if preserved_member(name, data):
                    if not name.startswith(('lib/', 'assets/')) and name not in identity['preserved']:
                        raise ValueError('Unexpected new native payload outside the preserved inventory')
                    continue
                if re.fullmatch(r'META-INF/(?:[^/]+\.(?:RSA|DSA|EC|SF)|MANIFEST\.MF)', name, re.I):
                    continue
                output.writestr(item, data)
            for name, digest in identity['preserved'].items():
                data = old.read(name)
                if sha256(data) != digest:
                    raise ValueError('Original native/asset payload changed during UI rebuild')
                output.writestr(old_names[name], data)
    with zipfile.ZipFile(target) as archive:
        names = zip_inventory(archive)
        actual = {name: sha256(archive.read(name)) for name, item in names.items()
                  if not item.is_dir() and preserved_member(name, archive.read(name))}
        if actual != identity['preserved']:
            raise ValueError('UI-only unsigned APK did not preserve every native and asset byte')
        if manifest_identity(archive.read('AndroidManifest.xml')) != identity['manifest']:
            raise ValueError('Final unsigned APK identity changed')


def build(args):
    ui_repository, recipe_repository = args.ui_root.resolve(strict=True), args.recipe_repository.resolve(strict=True)
    work = args.work_dir.absolute()
    if work.exists() or work.is_symlink():
        raise ValueError('Maintenance work directory already exists')
    work.mkdir(parents=True)
    # A maintenance workflow has its own producer commit. Export the reviewed UI
    # commit rather than trusting whichever worktree/HEAD happens to launch it.
    ui_root = work / 'reviewed-ui'
    export_recipe(ui_repository, UI_COMMIT, ui_root)
    identity = original_identity(args.original_apk, args.expected_sha256, ui_root)
    recipe, source = work / 'historical-recipe', work / 'manager-source'
    export_recipe(recipe_repository, identity['metadata']['producer_head_sha'], recipe)
    restore_source(source, identity, recipe, args.source_repository)
    ui_changes = overlay_ui(source, identity, ui_root)
    gradle = prepare_gradle(source, work, identity)
    receipt = {'schema_version': 1, 'ui_commit': UI_COMMIT, 'original': identity,
               'historical_recipe': identity['metadata']['producer_head_sha'],
               'manager_source': identity['metadata']['ksu_ref'], 'ui_changes': ui_changes,
               'gradle': gradle, 'prepared': True, 'compiled': False}
    (work / 'refresh-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    if args.prepare_only:
        return
    environment = {name: value for name, value in os.environ.items()
        if not name.startswith(('SUKISU_APK_', 'ORG_GRADLE_PROJECT_KEY', 'KEYSTORE_'))
        and name not in ('KEY_ALIAS', 'KEY_PASSWORD')}
    environment.update(KSU_VARIANT=identity['metadata']['ksu_variant'],
                       KSU_PACKAGE_NAME=identity['manifest']['package'])
    log = work / 'gradle-ui-only.log'
    command = ['bash', './gradlew', '--no-daemon', '--stacktrace', '--build-cache',
               '--init-script', str(Path(gradle['guard']).resolve()), ':app:assembleRelease',
               '-PKSU_PACKAGE_NAME=' + identity['manifest']['package']]
    with log.open('w', encoding='utf-8') as stream:
        process = subprocess.run(command, cwd=source / 'manager', env=environment,
                                 stdout=stream, stderr=subprocess.STDOUT, timeout=3600)
    if process.returncode:
        print('\n'.join(log.read_text('utf-8', errors='replace').splitlines()[-180:]), file=sys.stderr)
        raise RuntimeError('Historical UI Gradle build failed; inspect ' + str(log))
    outputs = list((source / 'manager/app/build/outputs/apk/release').rglob('*.apk'))
    if len(outputs) != 1:
        raise ValueError('Expected exactly one UI-only universal APK output')
    output = args.output_dir.absolute()
    output.mkdir(parents=True, exist_ok=True)
    target = output / 'manager-unsigned.apk'
    staged_name = args.original_apk.name.removesuffix('.apk') + '-unsigned.apk'
    staged = output / staged_name
    rebuild_archive(args.original_apk, outputs[0], staged, identity)
    shutil.copyfile(staged, target)
    build_metadata = {'source_apk_filename': staged_name, 'apk_filename': args.original_apk.name,
        'manager_package': identity['manifest']['package'],
        'manager_version_code': identity['manifest']['version_code'],
        'manager_version_name': identity['manifest']['version_name']}
    (output / 'manager-build-metadata.json').write_text(json.dumps(build_metadata) + '\n', encoding='utf-8')
    receipt.update(compiled=True, unsigned_sha256=sha256(target.read_bytes()),
                   compiled_apk=str(outputs[0]), unsigned_apk=str(target))
    (output / 'refresh-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original-apk', required=True, type=Path)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--recipe-repository', required=True, type=Path)
    parser.add_argument('--ui-root', required=True, type=Path)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--source-repository', help='Optional exact local upstream mirror for offline source restore')
    parser.add_argument('--prepare-only', action='store_true')
    build(parser.parse_args())


if __name__ == '__main__':
    main()

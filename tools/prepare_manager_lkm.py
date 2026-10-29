#!/usr/bin/env python3
"""Verify real same-source ARM64 LKMs before embedding them in the manager's ksud.

This implements the asset preparation in SukiSU's official ksud.yml. It does
not invent KMI entries, download unrelated official-signature modules, modify
module binaries, or claim that native LKM mode includes the inline SUSFS port.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import sys
import tempfile

from snapshot_ksu_source import SnapshotError, expected_commit, git, source_identity
from manager_lkm_jobs import load_ddk_matrix
from manager_variants import manager_profile, selected_variant


KMI = re.compile(r"android[0-9]+-[0-9]+\.[0-9]+\Z")
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")
MAX_MODULE = 64 * 1024 * 1024
MAX_JSON = 64 * 1024
ROOT = Path(__file__).resolve().parents[1]
_UNSPECIFIED_IMAGE = object()
KPM_RECORD = 'kernel/kpm/.kpm.o.cmd'
MAX_KPM_COMMAND = 32 * 1024
KPM_MODULE_BINDINGS = ('ksu_ref', 'ksu_version_code', 'bakasu_kpm_port_sha256', 'sha256', 'size',
                       'kmi', 'ddk_image', 'manager_package', 'manager_cert_size', 'manager_cert_sha256')


class LkmError(ValueError):
    pass


def locked_ddk(kmi: str, image: object = _UNSPECIFIED_IMAGE) -> dict:
    """Bind a KMI to the reviewed immutable image and its actual UTS_RELEASE."""
    try:
        candidates = [entry for entry in load_ddk_matrix(ROOT) if entry['kmi'] == kmi]
    except (OSError, ValueError) as error:
        raise LkmError('The reviewed manager DDK image/release lock is invalid') from error
    if len(candidates) != 1 or image is not _UNSPECIFIED_IMAGE and image != candidates[0]['image']:
        raise LkmError('LKM DDK image does not match its declared KMI and pinned image')
    return candidates[0]


def read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_JSON:
        raise LkmError("LKM public metadata is missing or invalid")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as error:
        raise LkmError("LKM public metadata is not valid JSON") from error
    if not isinstance(document, dict):
        raise LkmError("LKM public metadata must be an object")
    return document


def public_identity(path: Path) -> dict:
    document = read_json(path)
    package = document.get("manager_package")
    digest = document.get("manager_cert_sha256")
    size = document.get("manager_cert_size")
    if (not isinstance(package, str) or not PACKAGE.fullmatch(package)
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or isinstance(size, bool) or not isinstance(size, (str, int))
            or not re.fullmatch(r"[1-9][0-9]*", str(size)) or not 64 <= int(size) <= 4096):
        raise LkmError("LKM requires the actual manager package and public signing certificate identity")
    return {"manager_package": package, "manager_cert_size": int(size), "manager_cert_sha256": digest}


def selected_source(source: Path, reference: str) -> dict:
    variant = selected_variant()
    reference = expected_commit(reference)
    metadata = source_identity(source, reference)
    # The restored BakaSU KPM bridge is a verified additive source port. Inline
    # SUSFS or any unrelated native-source change must still be rejected.
    if variant == 'BakaSU':
        from restore_bakasu_kpm import verify_bakasu_kpm_port
        try:
            port = verify_bakasu_kpm_port(source, reference,
                                         scope=('kernel', 'uapi', 'userspace/ksud/src/assets.rs'))
        except ValueError as error:
            raise LkmError('BakaSU LKM source port verification failed: ' + str(error)) from None
        metadata['bakasu_kpm_port_sha256'] = port['port_sha256']
    else:
        git(source, "diff", "--quiet", "HEAD", "--", "kernel", "uapi", "userspace/ksud/src/assets.rs")
    version = native_version_code(source, metadata["git_count"], variant=variant)
    return {"repository": manager_profile(variant)['repository'], "ksu_ref": reference,
            "ksu_version_code": version, **metadata}


def native_version_code(source: Path, count: int, *, variant: str | None = None) -> int:
    """Use the exact upstream formula with this immutable HEAD's history.

    Native Kbuild otherwise counts the moving `main` branch or queries GitHub.
    Neither is appropriate for a detached, fully offline build of a snapshot.
    """
    path = source / "kernel/Kbuild"
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 1024 * 1024:
        raise LkmError("Native KernelSU version formula is missing or unsafe")
    text = path.read_text(encoding="utf-8")
    if selected_variant(variant) == 'BakaSU':
        count_lines = re.findall(r'^KSU_LOCAL_VERSION[ \t]*:=[^\r\n]+', text, re.M)
        formula = re.findall(r'^KSU_VERSION[ \t]*:=[^\r\n]+', text, re.M)
        expected_count = 'KSU_LOCAL_VERSION := $(shell cd $(KSU_SRC); $(GIT_BIN) rev-list --count HEAD)'
        expected_formula = 'KSU_VERSION := $(shell expr 30000 + $(KSU_LOCAL_VERSION) + 700)'
        if (len(count_lines) != 1 or len(formula) != 1
                or re.sub(r'[ \t]+', ' ', count_lines[0]).strip() != expected_count
                or re.sub(r'[ \t]+', ' ', formula[0]).strip() != expected_formula
                or type(count) is not int or count < 1):
            raise LkmError('Native BakaSU version formula changed; review its history calculation')
        version = 30700 + count
        if not 1 <= version <= 2100000000:
            raise LkmError('The native BakaSU source history produced an invalid version code')
        return version
    values = {}
    for key in ("VERSION_BASE", "VERSION_OFFSET"):
        found = re.findall(r"^" + key + r"[ \t]*:=[ \t]*([0-9]+)[ \t]*$", text, re.M)
        if len(found) != 1:
            raise LkmError("Native KernelSU version formula changed; review its history calculation")
        values[key] = int(found[0])
    formula = re.findall(r"^KSU_VERSION[ \t]*:=[^\r\n]+", text, re.M)
    if (len(formula) != 1 or "$(shell expr $(VERSION_BASE) + $(LOCAL_COUNT) - $(VERSION_OFFSET))" not in formula[0]
            or type(count) is not int or count < 1):
        raise LkmError("Native KernelSU version formula changed; review its history calculation")
    version = values["VERSION_BASE"] + count - values["VERSION_OFFSET"]
    if not 1 <= version <= 2100000000:
        raise LkmError("The native KernelSU source history produced an invalid version code")
    return version


def compile_version_proof(source: Path, version: int) -> dict:
    """Check actual Kbuild compiler records before accepting a stripped module.

    Upstream exposes the numeric version as ARM64 instructions, not a modinfo
    version string. The unchanged native source, exact compiler defines and
    final ELF digest bind the producer's version without guessing machine-code
    encodings or inserting a synthetic version string into the module.
    """
    evidence = []
    for path in sorted((source / "kernel").rglob("*.cmd")):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise LkmError("Native module compiler evidence is unsafe")
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        versions = re.findall(r"(?:^|[ \t])-DKSU_VERSION=([0-9]+)(?=[ \t]|$)", text, re.M)
        if not versions:
            continue
        if any(int(value) != version for value in versions):
            raise LkmError("The actual native module compiler version differs from its pinned source history")
        evidence.append({"file": path.relative_to(source).as_posix(), "sha256": hashlib.sha256(raw).hexdigest()})
    if not evidence:
        raise LkmError("Native module is missing actual compiler version evidence")
    return {"compiler_records": len(evidence),
            "compiler_records_sha256": hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()}


def validate_kpm_command(command: str, version: int) -> None:
    """Require the real KPM translation unit, rather than an unrelated -D flag."""
    if (not isinstance(command, str) or not 0 < len(command.encode('utf-8')) <= MAX_KPM_COMMAND
            or any(character in command for character in ('\0', '\n', '\r'))):
        raise LkmError('BakaSU LKM KPM compiler command is missing or unsafe')
    if type(version) is not int or not 1 <= version <= 2_100_000_000:
        raise LkmError('BakaSU LKM KPM compiler evidence requires a pinned numeric version')
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|')
        lexer.whitespace_split, lexer.commenters = True, ''
        tokens = list(lexer)
    except ValueError as error:
        raise LkmError('BakaSU LKM KPM compiler command is invalid') from error
    # Kbuild may append an objtool command after the actual C compilation.
    end = next((index for index, token in enumerate(tokens) if token in (';', '&&', '||', '|', '&')), len(tokens))
    compile_tokens = tokens[:end]
    # Match the preprocessor identifier, not an option prefix: native BakaSU
    # emits both KSU_VERSION and the unrelated KSU_VERSION_FULL string.
    # Repeated identical defines are harmless; undefines or conflicting
    # values cannot establish the pinned translation unit's configuration.
    macros = {'CONFIG_KPM': [], 'KSU_VERSION': []}
    options, forwarded = [], False
    for token in compile_tokens:
        if token == '-Xpreprocessor':
            if forwarded:
                raise LkmError('BakaSU LKM KPM compiler evidence has an incomplete preprocessor option')
            forwarded = True
            continue
        if token.startswith('@'):
            raise LkmError('BakaSU LKM KPM compiler evidence has an unexpanded response file')
        if token.startswith('-Wp,'):
            options.extend(token[4:].split(','))
        else:
            options.append(token)
        forwarded = False
    if forwarded:
        raise LkmError('BakaSU LKM KPM compiler evidence has an incomplete preprocessor option')
    index = 0
    while index < len(options):
        token = options[index]
        index += 1
        if token in ('-D', '-U'):
            if index == len(options):
                raise LkmError('BakaSU LKM KPM compiler evidence has an incomplete macro option')
            body = options[index]
            index += 1
            operation = token
        elif token.startswith(('-D', '-U')):
            operation, body = token[:2], token[2:]
        else:
            continue
        identifier = re.match(r'[A-Za-z_][A-Za-z0-9_]*', body)
        if identifier is None or identifier[0] not in macros:
            continue
        name = identifier[0]
        suffix = body[len(name):]
        if operation == '-U':
            raise LkmError('BakaSU LKM KPM compiler evidence undefines a required macro: ' + name)
        value = '1' if not suffix else suffix[1:] if suffix.startswith('=') else ''
        macros[name].append(value)
    for name, expected in (('CONFIG_KPM', '1'), ('KSU_VERSION', str(version))):
        values = macros[name]
        if not values:
            raise LkmError('BakaSU LKM KPM compiler evidence is missing the required macro: ' + name)
        if any(value != expected for value in values):
            raise LkmError('BakaSU LKM KPM compiler evidence has a conflicting or disabled macro: ' + name)
    outputs = [compile_tokens[index + 1] for index, token in enumerate(compile_tokens[:-1]) if token == '-o']
    sources = [token for token in compile_tokens if token == 'kpm/kpm.c' or token.endswith('/kpm/kpm.c')]
    if compile_tokens.count('-c') != 1:
        raise LkmError('BakaSU LKM KPM compiler evidence is missing one C compilation operation')
    if len(outputs) != 1 or not (outputs[0] == 'kpm/kpm.o' or outputs[0].endswith('/kpm/kpm.o')):
        raise LkmError('BakaSU LKM KPM compiler evidence has a different output object')
    if len(sources) != 1:
        raise LkmError('BakaSU LKM KPM compiler evidence is missing its native source translation unit')


def kpm_proof_binding(proof: dict, metadata: dict) -> str:
    body = {'proof': proof, 'module': {key: metadata[key] for key in KPM_MODULE_BINDINGS}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def compiled_kpm_proof(source: Path, metadata: dict) -> dict:
    """Transport the bounded actual Kbuild recipe with its module/port binding.

    A cache consumer has no compiler worktree. It revalidates this command and
    binding before accepting the recorded ELF, rather than trusting a build
    input flag or a boolean capability claim. Runtime capability is still
    determined by the real ABI-2 payload probe on the device.
    """
    path = source / KPM_RECORD
    if (any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(source))
            or not path.is_file() or not 0 < path.stat().st_size <= 8 * 1024 * 1024):
        raise LkmError('BakaSU LKM is missing its actual native KPM compiler record')
    raw = path.read_bytes()
    text = re.sub(r'\\\r?\n', ' ', raw.decode('utf-8'))
    commands = re.findall(r'^(?:savedcmd|cmd)_[^\r\n]*kpm/kpm\.o[ \t]*:=[ \t]*(.+)$', text, re.M)
    if len(commands) != 1:
        raise LkmError('BakaSU LKM requires exactly one native KPM compiler command')
    command = commands[0].strip()
    validate_kpm_command(command, metadata['ksu_version_code'])
    proof = {'schema_version': 1, 'record_file': KPM_RECORD,
             'record_sha256': hashlib.sha256(raw).hexdigest(), 'command': command}
    return {**proof, 'binding_sha256': kpm_proof_binding(proof, metadata)}


def validate_compiled_kpm_proof(metadata: dict) -> None:
    proof = metadata.get('kpm_compiler_proof')
    if (not isinstance(proof, dict)
            or set(proof) != {'schema_version', 'record_file', 'record_sha256', 'command', 'binding_sha256'}
            or type(proof['schema_version']) is not int or proof['schema_version'] != 1
            or proof['record_file'] != KPM_RECORD
            or any(not isinstance(proof[field], str) or not re.fullmatch(r'[0-9a-f]{64}', proof[field])
                   for field in ('record_sha256', 'binding_sha256'))):
        raise LkmError('BakaSU LKM cache lacks verified native KPM compiler evidence; rebuild this module')
    validate_kpm_command(proof['command'], metadata['ksu_version_code'])
    core = {key: value for key, value in proof.items() if key != 'binding_sha256'}
    if proof['binding_sha256'] != kpm_proof_binding(core, metadata):
        raise LkmError('BakaSU LKM KPM compiler evidence does not match its module and reviewed port')


def inspect_module(path: Path, kmi: str, identity: dict, reference: str) -> dict:
    if not KMI.fullmatch(kmi):
        raise LkmError("Invalid LKM KMI")
    if path.is_symlink() or not path.is_file() or not 64 <= path.stat().st_size <= MAX_MODULE:
        raise LkmError("LKM must be a nonempty bounded regular file")
    data = path.read_bytes()
    if data[:7] != b"\x7fELF\x02\x01\x01":
        raise LkmError("LKM must be a little-endian ELF64 object")
    header = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    elf_type, machine, version = header[:3]
    section_offset, section_size, section_count, strings_index = header[5], header[10], header[11], header[12]
    if (elf_type != 1 or machine != 183 or version != 1 or header[7] != 64
            or section_size != 64 or not 1 < section_count <= 65535
            or not 0 < strings_index < section_count or section_offset < 64
            or section_offset + section_size * section_count > len(data)):
        raise LkmError("LKM must be an AArch64 relocatable object with a valid section table")
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, section_offset + index * section_size)
                for index in range(section_count)]
    def section_bytes(section):
        offset, size = section[4], section[5]
        if offset > len(data) or size > len(data) - offset:
            raise LkmError("LKM ELF section extends outside the module")
        return data[offset:offset + size]
    names = section_bytes(sections[strings_index])
    modinfo, allocated = [], []
    for section in sections:
        offset = section[0]
        end = names.find(b"\0", offset)
        if offset >= len(names) or end < offset:
            raise LkmError("LKM ELF section name is invalid")
        if section[1] == 8:  # SHT_NOBITS has no file-backed data.
            continue
        content = section_bytes(section)
        if names[offset:end] == b".modinfo":
            modinfo.append(content)
        if section[2] & 2:  # SHF_ALLOC: constants used by the loaded module.
            allocated.append(content)
    if len(modinfo) != 1:
        raise LkmError("LKM must contain exactly one real .modinfo section")
    fields = {}
    for entry in modinfo[0].split(b"\0"):
        if b"=" in entry:
            key, value = entry.split(b"=", 1)
            fields.setdefault(key, []).append(value)
    if fields.get(b"name") != [b"kernelsu"] or len(fields.get(b"vermagic", [])) != 1:
        raise LkmError("LKM .modinfo must identify kernelsu and one vermagic")
    try:
        vermagic = fields[b"vermagic"][0].decode("ascii")
    except UnicodeError as error:
        raise LkmError("LKM vermagic is invalid") from error
    # Seven of the eight official DDKs have a generic UTS_RELEASE, with no
    # Android name in it. Match the exact release reviewed for this pinned
    # image instead of inventing an Android suffix or accepting any same-series
    # module. The immutable image/KMI binding is also checked by record/stage.
    actual_release = vermagic.split(' ', 1)[0]
    expected_release = locked_ddk(kmi)['kernel_release']
    if not actual_release or actual_release != expected_release:
        raise LkmError(f'LKM vermagic release {actual_release[:128]!r} does not match '
                       f'pinned DDK kernel release {expected_release!r} for KMI {kmi}')
    # Native Kbuild emits these exact strings into loaded constants. This also
    # rejects official static-signature modules mistakenly substituted here.
    for label, value in (("manager package", identity["manager_package"]),
                         ("manager certificate", identity["manager_cert_sha256"]),
                         ("source revision", reference[:7])):
        if not any(value.encode("ascii") in content for content in allocated):
            raise LkmError(f"LKM does not contain the expected native {label} binding")
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "vermagic": vermagic}


def atomic_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".gki-lkm-", suffix=".json", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def record(source: Path, module: Path, kmi: str, identity_file: Path, reference: str,
           output: Path, ddk_image: str) -> dict:
    source = source.resolve(strict=True)
    identity = public_identity(identity_file)
    if identity['manager_package'] != manager_profile(selected_variant())['package']:
        raise LkmError('LKM manager package does not match the selected manager variant')
    provenance = selected_source(source, reference)
    locked_ddk(kmi, ddk_image)
    inspected = inspect_module(module, kmi, identity, provenance["ksu_ref"])
    version_proof = compile_version_proof(source, provenance["ksu_version_code"])
    filename = kmi + "_kernelsu.ko"
    metadata = {"schema_version": 1, "arch": "aarch64", "kmi": kmi, "file": filename,
                "ddk_image": ddk_image, **provenance, **identity, **inspected, **version_proof}
    if selected_variant() == 'BakaSU':
        metadata['kpm_compiler_proof'] = compiled_kpm_proof(source, metadata)
        validate_compiled_kpm_proof(metadata)
        if len(json.dumps(metadata, ensure_ascii=True, indent=2).encode('utf-8')) + 1 > MAX_JSON:
            raise LkmError('BakaSU LKM compiler evidence exceeds its bounded cache metadata')
    output = output.absolute()
    output.mkdir(parents=True, exist_ok=True)
    if output.is_symlink() or (output / filename).exists() or (output / "module.json").exists():
        raise LkmError("LKM artifact output already exists or is symlinked")
    shutil.copyfile(module, output / filename)
    if hashlib.sha256((output / filename).read_bytes()).hexdigest() != inspected["sha256"]:
        raise LkmError("LKM changed while recording its artifact")
    atomic_json(output / "module.json", metadata)
    return metadata


def stage(source: Path, artifacts: Path, identity_file: Path, reference: str,
          expected_kmis: str, report: Path) -> dict:
    source, artifacts = source.resolve(strict=True), artifacts.resolve(strict=True)
    identity = public_identity(identity_file)
    if identity['manager_package'] != manager_profile(selected_variant())['package']:
        raise LkmError('LKM manager package does not match the selected manager variant')
    provenance = selected_source(source, reference)
    required = expected_kmis.split(",")
    if (not 1 <= len(required) <= 32 or len(required) != len(set(required))
            or any(not KMI.fullmatch(kmi) for kmi in required)):
        raise LkmError("Expected KMIs must be a complete unique comma-separated list")
    manifests = sorted(artifacts.rglob("module.json"))
    if len(manifests) != len(required):
        raise LkmError("LKM artifact set is incomplete or contains duplicate manifests")
    verified, found = [], set()
    for path in manifests:
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(artifacts)):
            raise LkmError("LKM artifact paths must not contain symlinks")
        metadata = read_json(path)
        kmi = metadata.get("kmi")
        if (type(metadata.get("schema_version")) is not int or metadata["schema_version"] != 1
                or metadata.get("arch") != "aarch64" or kmi not in required or kmi in found
                or metadata.get("file") != kmi + "_kernelsu.ko"
                or any(metadata.get(key) != value for key, value in {**identity, **provenance}.items())):
            raise LkmError("LKM source/version, manager identity or KMI provenance does not match this build")
        module = path.parent / metadata["file"]
        locked_ddk(kmi, metadata.get('ddk_image', ''))
        if (type(metadata.get("compiler_records")) is not int or metadata["compiler_records"] < 1
                or not isinstance(metadata.get("compiler_records_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", metadata["compiler_records_sha256"])):
            raise LkmError("LKM artifact lacks verified native compiler version evidence")
        if selected_variant() == 'BakaSU':
            validate_compiled_kpm_proof(metadata)
        actual = inspect_module(module, kmi, identity, provenance["ksu_ref"])
        if any(metadata.get(key) != value for key, value in actual.items()):
            raise LkmError("LKM artifact digest or ELF identity changed after compilation")
        found.add(kmi)
        verified.append((module, metadata))
    destination = source / "userspace/ksud/bin/aarch64"
    if not destination.is_dir() or destination.resolve() != destination:
        raise LkmError("Official ARM64 ksud asset directory is missing or symlinked")
    existing = {path.name for path in destination.glob("*_kernelsu.ko")}
    if existing - {metadata["file"] for _, metadata in verified}:
        raise LkmError("Unexpected pre-existing LKM assets would add unverified KMI choices")
    for module, metadata in verified:
        target = destination / metadata["file"]
        if target.is_symlink() or (target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != metadata["sha256"]):
            raise LkmError("An existing ksud LKM asset does not match the verified module")
    # The full set has passed verification before any asset is copied.
    for module, metadata in verified:
        target = destination / metadata["file"]
        if not target.exists():
            shutil.copyfile(module, target)
        if inspect_module(target, metadata["kmi"], identity, provenance["ksu_ref"])["sha256"] != metadata["sha256"]:
            raise LkmError("Staged LKM does not match its verified artifact")
    result = {"schema_version": 1, "mode": "native-lkm", "supported_kmis": sorted(found),
              **provenance, **identity, "modules": [metadata for _, metadata in verified]}
    atomic_json(report, result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="operation", required=True)
    for name in ("record", "stage", "version-code"):
        command = operations.add_parser(name)
        command.add_argument("--source", required=True, type=Path)
        if name != "version-code":
            command.add_argument("--identity", required=True, type=Path)
        command.add_argument("--expected-ref", required=True)
        if name == "record":
            command.add_argument("--module", required=True, type=Path)
            command.add_argument("--kmi", required=True)
            command.add_argument("--output", required=True, type=Path)
            command.add_argument("--ddk-image", required=True)
        elif name == "stage":
            command.add_argument("--artifacts", required=True, type=Path)
            command.add_argument("--expected-kmis", required=True)
            command.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.operation == "version-code":
            print(selected_source(args.source.resolve(strict=True), args.expected_ref)["ksu_version_code"])
        elif args.operation == "record":
            result = record(args.source, args.module, args.kmi, args.identity, args.expected_ref, args.output, args.ddk_image)
            print("Verified native LKM: " + result["file"])
        else:
            result = stage(args.source, args.artifacts, args.identity, args.expected_ref, args.expected_kmis, args.report)
            print("Verified embedded KMI modules: " + ", ".join(result["supported_kmis"]))
    except (LkmError, SnapshotError, OSError, UnicodeError, struct.error) as error:
        print("::error::Manager LKM preparation stopped: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

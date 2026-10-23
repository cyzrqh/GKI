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
import shutil
import struct
import sys
import tempfile

from snapshot_ksu_source import REPOSITORY, SnapshotError, expected_commit, git, source_identity


KMI = re.compile(r"android[0-9]+-[0-9]+\.[0-9]+\Z")
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")
MAX_MODULE = 64 * 1024 * 1024
MAX_JSON = 64 * 1024


class LkmError(ValueError):
    pass


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
    reference = expected_commit(reference)
    metadata = source_identity(source, reference)
    # An LKM uses upstream's original native hooks. The inline SUSFS integration
    # deliberately edits these files and must never be fed to this DDK build.
    git(source, "diff", "--quiet", "HEAD", "--", "kernel", "uapi", "userspace/ksud/src/assets.rs")
    version = native_version_code(source, metadata["git_count"])
    return {"repository": REPOSITORY, "ksu_ref": reference, "ksu_version_code": version, **metadata}


def native_version_code(source: Path, count: int) -> int:
    """Use the exact upstream formula with this immutable HEAD's history.

    Native Kbuild otherwise counts the moving `main` branch or queries GitHub.
    Neither is appropriate for a detached, fully offline build of a snapshot.
    """
    path = source / "kernel/Kbuild"
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 1024 * 1024:
        raise LkmError("Native KernelSU version formula is missing or unsafe")
    text = path.read_text(encoding="utf-8")
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
    android, kernel = kmi.split("-", 1)
    if not vermagic.startswith(kernel + ".") or not re.search(r"(?:^|[-_])" + re.escape(android) + r"(?:[-_ .]|$)", vermagic):
        raise LkmError("LKM vermagic does not match its declared KMI")
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
    provenance = selected_source(source, reference)
    if not re.fullmatch(r"ghcr\.io/ylarod/ddk-min(?::[A-Za-z0-9._-]+)?(?:@sha256:[0-9a-f]{64})?", ddk_image):
        raise LkmError("LKM DDK image must identify the official DDK container")
    inspected = inspect_module(module, kmi, identity, provenance["ksu_ref"])
    version_proof = compile_version_proof(source, provenance["ksu_version_code"])
    filename = kmi + "_kernelsu.ko"
    output = output.absolute()
    output.mkdir(parents=True, exist_ok=True)
    if output.is_symlink() or (output / filename).exists() or (output / "module.json").exists():
        raise LkmError("LKM artifact output already exists or is symlinked")
    metadata = {"schema_version": 1, "arch": "aarch64", "kmi": kmi, "file": filename,
                "ddk_image": ddk_image, **provenance, **identity, **inspected, **version_proof}
    shutil.copyfile(module, output / filename)
    if hashlib.sha256((output / filename).read_bytes()).hexdigest() != inspected["sha256"]:
        raise LkmError("LKM changed while recording its artifact")
    atomic_json(output / "module.json", metadata)
    return metadata


def stage(source: Path, artifacts: Path, identity_file: Path, reference: str,
          expected_kmis: str, report: Path) -> dict:
    source, artifacts = source.resolve(strict=True), artifacts.resolve(strict=True)
    identity = public_identity(identity_file)
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
        if (type(metadata.get("compiler_records")) is not int or metadata["compiler_records"] < 1
                or not isinstance(metadata.get("compiler_records_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", metadata["compiler_records_sha256"])):
            raise LkmError("LKM artifact lacks verified native compiler version evidence")
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

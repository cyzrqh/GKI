#!/usr/bin/env python3
"""Public build identity inside the final APK/AnyKernel3 files themselves.

APK v2 parsing/digests follow Android's published format:
https://source.android.com/docs/security/features/apksigning/v2
The signing step additionally runs Android apksigner's complete verification.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import zipfile

APK_MEMBER = 'assets/gki-build.json'
KERNEL_MEMBER = 'META-INF/gki/build.json'
MAX_APK_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024


def unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate embedded build metadata field')
        result[key] = value
    return result


def metadata_json(raw: bytes) -> dict:
    if not raw or len(raw) > MAX_METADATA_BYTES:
        raise ValueError('Embedded build metadata has an invalid size')
    def invalid_constant(_):
        raise ValueError('Invalid number in embedded build metadata')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('Embedded build metadata must contain one object')
    return value


def validate_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    result = {}
    if len(archive.infolist()) > 10000:
        raise ValueError('Unexpectedly many APK/archive entries')
    for item in archive.infolist():
        name = item.filename
        parts = name.rstrip('/').split('/')
        if (not name or item.orig_filename != name or name.startswith('/') or '\\' in name or ':' in name
                or any(part in ('', '.', '..') for part in parts)
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or PurePosixPath(name).is_absolute() or name in result
                or stat.S_IFMT(item.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)
                or item.flag_bits & 1):
            raise ValueError('Unsafe or duplicate archive member')
        result[name] = item
    for name, item in result.items():
        parts = name.rstrip('/').split('/')
        for position in range(1, len(parts)):
            parent = '/'.join(parts[:position])
            if parent in result and not result[parent].is_dir():
                raise ValueError('Archive file shadows a required directory')
        if name.rstrip('/') + '/' in result and not item.is_dir():
            raise ValueError('Conflicting archive file and directory')
    return result


def read_member(archive: zipfile.ZipFile, member: str) -> dict:
    members = validate_members(archive)
    entry = members.get(member)
    if entry is None or entry.is_dir() or not 0 < entry.file_size <= MAX_METADATA_BYTES:
        raise ValueError('Missing or oversized embedded build metadata')
    return metadata_json(archive.read(entry))


def _length_prefixed(data: bytes, position: int = 0) -> tuple[bytes, int]:
    if position < 0 or position + 4 > len(data):
        raise ValueError('Truncated APK signing record')
    length = struct.unpack_from('<I', data, position)[0]
    end = position + 4 + length
    if end > len(data):
        raise ValueError('Invalid APK signing record length')
    return data[position + 4:end], end


def _records(data: bytes):
    position = 0
    while position < len(data):
        value, position = _length_prefixed(data, position)
        yield value


def _algorithm_records(data: bytes) -> dict[int, bytes]:
    result = {}
    for value in _records(data):
        if len(value) < 8:
            raise ValueError('Truncated APK signature algorithm record')
        algorithm = struct.unpack_from('<I', value)[0]
        record, end = _length_prefixed(value, 4)
        if end != len(value) or algorithm in result:
            raise ValueError('Duplicate or malformed APK signature algorithm record')
        result[algorithm] = record
    return result


def verify_apk_v2(data: bytes) -> bytes:
    """Verify RSA/SHA256 signer, X509 key agreement and Android chunk digests.

    This repository uses one fixed RSA key and Android's v2 RSA PKCS#1 SHA256
    algorithm (0x0103). Reject unsupported identities rather than accepting
    only a certificate fingerprint without checking the APK signature.
    """
    if not 0 < len(data) <= MAX_APK_BYTES:
        raise ValueError('Unexpected signed APK size')
    candidates = [pos for pos in range(max(0, len(data) - 65557), len(data) - 21)
                  if data[pos:pos + 4] == b'PK\x05\x06'
                  and pos + 22 + struct.unpack_from('<H', data, pos + 20)[0] == len(data)]
    if len(candidates) != 1:
        raise ValueError('Missing or ambiguous APK ZIP end record')
    eocd = candidates[0]
    disk, directory_disk, count_disk, count, directory_size, directory_offset = struct.unpack_from('<HHHHII', data, eocd + 4)
    if (disk or directory_disk or count_disk != count or count == 0xFFFF
            or directory_offset + directory_size != eocd or directory_offset < 32
            or data[directory_offset - 16:directory_offset] != b'APK Sig Block 42'):
        raise ValueError('Unsupported or invalid APK ZIP/signing layout')
    block_size = struct.unpack_from('<Q', data, directory_offset - 24)[0]
    block_start = directory_offset - block_size - 8
    if (not 24 <= block_size <= 1024 * 1024 or block_start < 0
            or struct.unpack_from('<Q', data, block_start)[0] != block_size):
        raise ValueError('APK signing block length mismatch')
    pairs = {}
    position = block_start + 8
    pair_end = directory_offset - 24
    while position < pair_end:
        if position + 12 > pair_end:
            raise ValueError('Truncated APK signing pair')
        length, identity = struct.unpack_from('<QI', data, position)
        end = position + 8 + length
        if length < 4 or end > pair_end or identity in pairs:
            raise ValueError('Duplicate or invalid APK signing pair')
        pairs[identity] = data[position + 12:end]
        position = end
    if position != pair_end or 0x7109871A not in pairs:
        raise ValueError('The APK has no valid v2 signing block')
    signers, end = _length_prefixed(pairs[0x7109871A])
    if end != len(pairs[0x7109871A]):
        raise ValueError('Unexpected trailing v2 signing data')
    signers = list(_records(signers))
    if len(signers) != 1:
        raise ValueError('Exactly one manager APK signer is required')
    signed_data, position = _length_prefixed(signers[0])
    signatures, position = _length_prefixed(signers[0], position)
    public_key, position = _length_prefixed(signers[0], position)
    if position != len(signers[0]):
        raise ValueError('Unexpected signer record data')
    digests, position = _length_prefixed(signed_data)
    certificates, position = _length_prefixed(signed_data, position)
    attributes, position = _length_prefixed(signed_data, position)
    # Current Android apksigner can append an empty length-prefixed padding
    # field. It is part of signed_data and is covered by the RSA signature.
    if signed_data[position:] not in (b'', b'\0' * 4):
        raise ValueError('Unexpected signed APK metadata')
    for attribute in _records(attributes):
        if len(attribute) < 4:
            raise ValueError('Malformed signed APK additional attribute')
        identity = struct.unpack_from('<I', attribute)[0]
        # Android v2 stripping protection: this signer also promises a v3
        # signature, so removing that block must not leave an accepted APK.
        if identity == 0xBEEFF00D and (len(attribute) != 8
                or struct.unpack_from('<I', attribute, 4)[0] != 3 or 0xF05368C0 not in pairs):
            raise ValueError('APK signature scheme stripping protection failed')
    certificates = list(_records(certificates))
    if len(certificates) != 1 or not 64 <= len(certificates[0]) <= 4096:
        raise ValueError('Unexpected APK signer certificate chain')
    signature_map, digest_map = _algorithm_records(signatures), _algorithm_records(digests)
    if list(signature_map) != list(digest_map) or 0x0103 not in signature_map:
        raise ValueError('APK RSA/SHA256 signatures and digests do not agree')
    end_record = bytearray(data[eocd:])
    struct.pack_into('<I', end_record, 16, block_start)
    chunks = []
    for section in (data[:block_start], data[directory_offset:eocd], bytes(end_record)):
        for offset in range(0, len(section), 1024 * 1024):
            part = section[offset:offset + 1024 * 1024]
            chunks.append(hashlib.sha256(b'\xa5' + struct.pack('<I', len(part)) + part).digest())
    expected_digest = hashlib.sha256(b'\x5a' + struct.pack('<I', len(chunks)) + b''.join(chunks)).digest()
    if digest_map[0x0103] != expected_digest:
        raise ValueError('APK v2 content digest verification failed')
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        certificate = x509.load_der_x509_certificate(certificates[0])
        key = certificate.public_key()
        if (not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048
                or key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != public_key):
            raise ValueError('APK certificate and RSA public key disagree')
        key.verify(signature_map[0x0103], signed_data, padding.PKCS1v15(), hashes.SHA256())
    except ImportError:
        raise ValueError('Python cryptography is required to verify the signed manager APK') from None
    except ValueError:
        raise
    except Exception:
        raise ValueError('APK v2 RSA signature verification failed') from None
    return certificates[0]


def validate_manager_document(metadata: dict) -> None:
    def positive(value):
        return (type(value) in (str, int) and re.fullmatch(r'[1-9][0-9]*', str(value))
                and int(value) <= 2**63 - 1)
    if (type(metadata.get('schema_version')) is not int or metadata['schema_version'] != 1
            or metadata.get('ksu_variant') != 'SukiSU' or 'apk_sha256' in metadata or 'sha256' in metadata):
        raise ValueError('Invalid or self-referential embedded manager identity')
    for field in ('ksu_ref', 'producer_head_sha'):
        if not isinstance(metadata.get(field), str) or not re.fullmatch(r'[0-9a-f]{40}', metadata[field]):
            raise ValueError('Embedded manager identity lacks immutable source/workflow hashes')
    package, branch, version = (metadata.get(field) for field in ('package', 'ksu_branch', 'ksu_version_name'))
    if (not isinstance(package, str) or len(package) > 255
            or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', package)
            or not isinstance(branch, str) or not branch or len(branch) > 200
            or not isinstance(version, str) or not version or len(version) > 128
            or any(not 32 <= ord(c) <= 126 for c in version)
            or any(ord(c) < 32 or ord(c) == 127 for c in branch)):
        raise ValueError('Invalid embedded manager package, branch or version')
    if (not positive(metadata.get('ksu_version_code')) or int(metadata['ksu_version_code']) > 2100000000
            or not positive(metadata.get('producer_run_id')) or not positive(metadata.get('producer_run_attempt'))
            or not isinstance(metadata.get('producer_repository'), str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', metadata['producer_repository'])):
        raise ValueError('Invalid embedded manager version or workflow provenance')


def read_manager_apk(source: Path | bytes, *, filename: str | None = None) -> dict:
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_APK_BYTES:
            raise ValueError('Expected a regular final manager APK')
        data, filename = path.read_bytes(), filename or path.name
    else:
        data = bytes(source)
    certificate = verify_apk_v2(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = validate_members(archive)
        for required in ('AndroidManifest.xml', 'classes.dex', 'lib/arm64-v8a/libksud.so'):
            if required not in members or not 0 < members[required].file_size <= 128 * 1024 * 1024:
                raise ValueError('Manager APK lacks a required nonempty runtime file')
        if sum(member.file_size for member in members.values()) > 256 * 1024 * 1024 or archive.testzip():
            raise ValueError('Manager APK ZIP integrity verification failed')
        if APK_MEMBER not in members or members[APK_MEMBER].file_size > 16 * 1024:
            raise ValueError('Manager build identity exceeds the metadata size limit')
        metadata = read_member(archive, APK_MEMBER)
    validate_manager_document(metadata)
    name = metadata.get('file')
    if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]*\.apk', name)
            or '..' in name or len(name) > 250 or filename is not None and filename != name):
        raise ValueError('Embedded manager APK filename does not match its final artifact')
    native = re.fullmatch(r'SukiSU_.+_([0-9]+)-release\.apk', name)
    if native and int(native[1]) != int(metadata['ksu_version_code']):
        raise ValueError('Embedded manager version code differs from its native APK filename')
    if (str(metadata.get('manager_cert_size')) != str(len(certificate))
            or metadata.get('manager_cert_sha256') != hashlib.sha256(certificate).hexdigest()):
        raise ValueError('Embedded manager certificate differs from the actual APK v2 signer')
    return {**metadata, 'apk_sha256': hashlib.sha256(data).hexdigest()}

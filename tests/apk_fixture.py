"""Small ZIP fixtures with genuine test-only RSA APK-v2 signatures.

No production key, compiled kernel or loadable kernel module is synthesized.
The placeholder APK payload tests transport/signature integrity, not Android
installation. Signing SDK behavior is tested separately in manager signing.
"""
import datetime as dt
from functools import lru_cache
import hashlib
import io
import json
import struct
import zipfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID


@lru_cache(maxsize=1)
def material():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'GKI APK verifier test only')])
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(12345)
                   .not_valid_before(dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc))
                   .not_valid_after(dt.datetime(2040, 1, 1, tzinfo=dt.timezone.utc))
                   .sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.DER))
    return key, certificate


def manager_metadata(**changes):
    _, certificate = material()
    value = {'schema_version': 1, 'file': 'SukiSU_v4.2.0_40922-release.apk',
             'package': 'com.sukisu.ultra', 'ksu_ref': 'b' * 40,
             'ksu_branch': 'Dev(开发)', 'ksu_variant': 'SukiSU', 'ksu_version_code': '40922',
             'ksu_version_name': '4.2.0', 'manager_cert_size': str(len(certificate)),
             'manager_cert_sha256': hashlib.sha256(certificate).hexdigest(),
             'producer_repository': 'cyzrqh/gki', 'producer_run_id': '123',
             'producer_run_attempt': '1', 'producer_head_sha': 'a' * 40}
    value.update(changes)
    return value


def lp(data):
    return struct.pack('<I', len(data)) + data


def signed_apk(metadata=None, *, member='assets/gki-build.json', extra=False, raw_metadata=None,
               manifest_bytes=None):
    key, certificate = material()
    metadata = manager_metadata() if metadata is None else metadata
    raw_metadata = json.dumps(metadata, sort_keys=True).encode() if raw_metadata is None else raw_metadata
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        entries = [('AndroidManifest.xml', manifest_bytes if manifest_bytes is not None else b'APK manifest fixture'), ('classes.dex', b'APK dex fixture'),
                   ('lib/arm64-v8a/libksud.so', b'APK daemon fixture'), (member, raw_metadata)]
        if extra:
            entries.append(('../untrusted', b'unsafe APK member'))
        for name, value in entries:
            info = zipfile.ZipInfo(name)
            archive.writestr(info, value)
    unsigned = stream.getvalue()
    eocd = unsigned.rfind(b'PK\x05\x06')
    offset = struct.unpack_from('<I', unsigned, eocd + 16)[0]
    digests = []
    for section in (unsigned[:offset], unsigned[offset:eocd], unsigned[eocd:]):
        for position in range(0, len(section), 1024 * 1024):
            chunk = section[position:position + 1024 * 1024]
            digests.append(hashlib.sha256(b'\xa5' + struct.pack('<I', len(chunk)) + chunk).digest())
    digest = hashlib.sha256(b'\x5a' + struct.pack('<I', len(digests)) + b''.join(digests)).digest()
    signed = lp(lp(struct.pack('<I', 0x0103) + lp(digest))) + lp(lp(certificate)) + lp(b'')
    signature = key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    signer = lp(signed) + lp(lp(struct.pack('<I', 0x0103) + lp(signature))) + lp(public)
    value = lp(lp(signer))
    pair = struct.pack('<Q', 4 + len(value)) + struct.pack('<I', 0x7109871A) + value
    block_size = len(pair) + 24
    block = struct.pack('<Q', block_size) + pair + struct.pack('<Q', block_size) + b'APK Sig Block 42'
    end = bytearray(unsigned[eocd:])
    struct.pack_into('<I', end, 16, offset + len(block))
    return unsigned[:offset] + block + unsigned[offset:eocd] + end

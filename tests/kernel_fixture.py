"""Small, explicitly synthetic AK3/IKCONFIG fixtures for transport tests only."""
import gzip
import hashlib
import io
import json
import struct
import zipfile

from apk_fixture import manager_metadata


def arm64_fixture(payload):
    """A structurally valid entry header around inert, NON-BOOTABLE test bytes."""
    return struct.pack('<IIQQQQQQII', 0x14000010, 0, 0, 64 + len(payload), 10,
                       0, 0, 0, 0x644D5241, 0) + payload


def kernel_fixture(manager=None, *, file='android14-6.1.145-2025-08-AnyKernel3.zip'):
    manager = manager_metadata() if manager is None else manager
    target = file.removesuffix('-AnyKernel3.zip').split('-')
    series, sublevel = target[1].rsplit('.', 1)
    config = ('CONFIG_ARM64=y\nCONFIG_KSU=y\nCONFIG_KSU_SUSFS=y\nCONFIG_LTO_CLANG_THIN=y\n'
              '# CONFIG_KPM is not set\n# CONFIG_ZRAM is not set\n').encode()
    variant = manager.get('ksu_variant', 'SukiSU')
    if variant == 'BakaSU':
        config += b'CONFIG_KSU_MULTI_MANAGER_SUPPORT=y\n'
    # This is a test container payload, not a compiled or bootable kernel.
    image = arm64_fixture(b'TEST FIXTURE, NOT A KERNEL\0IKCFG_ST' + gzip.compress(config, mtime=0) + b'IKCFG_ED\0v2.3.0\0'
                          + manager['package'].encode() + b'\0' + manager['manager_cert_sha256'].encode() + b'\0')
    metadata = {'schema_version': 1, 'file': file, 'android_version': target[0],
                'kernel_version': series, 'sub_level': sublevel, 'os_patch_level': '-'.join(target[2:]),
                'ksu_variant': variant, 'ksu_branch': manager['ksu_branch'], 'ksu_ref': manager['ksu_ref'],
                'ksu_version_code': manager['ksu_version_code'], 'ksu_version_name': manager['ksu_version_name'],
                'susfs_version': 'v2.3.0', 'image_sha256': hashlib.sha256(image).hexdigest(),
                'config_sha256': hashlib.sha256(config).hexdigest(), 'config_source': 'Image / IKCONFIG',
                'features': {'susfs': True, 'kpm': False, 'bbg': False, 'zram': False, 'lz4kd': False,
                             'ntsync': False, 'rekernel': False, 'virtualization': False, 'ddk': False,
                             'lto': 'thin', 'hook': 'SUSFS Inline Hooks',
                             'mount': '由管理器/元模块在设备上设置'},
                'built_at_utc': '2026-10-24T00:00:00+00:00',
                'manager_package': manager['package'], 'manager_cert_size': manager['manager_cert_size'],
                'manager_cert_sha256': manager['manager_cert_sha256'], 'manager_apk_filename': manager['file'],
                'manager_apk_sha256': manager.get('apk_sha256', 'd' * 64), 'files': [file], 'boot_sha256': {}}
    for key in ('producer_repository', 'producer_run_id', 'producer_run_attempt', 'producer_head_sha'):
        if key in manager:
            metadata[key] = manager[key]
    return metadata, image


def ak3_bytes(metadata, image):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        for name, value in [('Image', image), ('anykernel.sh', b'test-anykernel-script'),
                            ('META-INF/com/google/android/update-binary', b'update-binary'),
                            ('META-INF/gki/build.json', json.dumps(metadata, ensure_ascii=False).encode())]:
            archive.writestr(zipfile.ZipInfo(name), value)
    return stream.getvalue()

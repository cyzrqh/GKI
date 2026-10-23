"""Build and privately transfer same-source, same-certificate native SukiSU LKMs."""
from __future__ import annotations

import json
from pathlib import Path
import re


KMIS = (
    'android12-5.10', 'android13-5.10', 'android13-5.15', 'android14-5.15',
    'android14-6.1', 'android15-6.6', 'android16-6.12', 'android17-6.18',
)
DDK_IMAGE = re.compile(r'ghcr\.io/ylarod/ddk-min(?::[A-Za-z0-9._-]+)?@sha256:([0-9a-f]{64})\Z')


def load_ddk_matrix(root: Path) -> list[dict[str, str]]:
    """Require a complete, immutable set of upstream's eight ARM64 DDK images."""
    path = Path(root) / 'config' / 'manager-ddk-images.json'
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 32768:
        raise ValueError('The manager DDK image lock must be a bounded regular file.')
    document = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(document, dict) or type(document.get('schema_version')) is not int
            or document['schema_version'] != 1 or not isinstance(document.get('images'), list)):
        raise ValueError('The manager DDK image lock must use schema_version 1 and an images list.')
    images = {}
    for entry in document['images']:
        if not isinstance(entry, dict) or set(entry) - {'kmi', 'image', 'ddk_short', 'tag'}:
            raise ValueError('Invalid manager DDK image entry.')
        kmi, image = entry.get('kmi'), entry.get('image')
        match = DDK_IMAGE.fullmatch(image) if isinstance(image, str) else None
        if not isinstance(kmi, str) or kmi not in KMIS or kmi in images or not match:
            raise ValueError('Every official KMI must have one fully pinned DDK image.')
        if ('tag' in entry and (not isinstance(entry['tag'], str)
                or not re.fullmatch(r'ghcr\.io/ylarod/ddk-min:' + re.escape(kmi) + r'-[0-9]{8}', entry['tag']))):
            raise ValueError('The human-readable DDK tag does not match its KMI.')
        short = match[1][:16]
        if 'ddk_short' in entry and entry['ddk_short'] != short:
            raise ValueError('The DDK image short digest does not match its pinned image.')
        images[kmi] = {'kmi': kmi, 'image': image, 'ddk_short': short}
    if set(images) != set(KMIS):
        raise ValueError('The manager requires all eight official KMI images.')
    return [images[kmi] for kmi in KMIS]


def lkm_cache_key(kmi_expr: str, ddk_short_expr: str) -> str:
    """Share an immutable cache key; arguments are raw GitHub expression contents."""
    def expression(value: str) -> str:
        if not isinstance(value, str) or not value or any(part in value for part in ('\n', '\r', '${{', '}}')):
            raise ValueError('Expected raw, single-line GitHub expression contents.')
        return '${{ ' + value + ' }}'
    fields = ('runner.os', 'runner.arch', 'needs.prepare-matrix.outputs.ksu_ref',
              'needs.prepare-matrix.outputs.manager_package', 'needs.prepare-matrix.outputs.manager_cert_size',
              'needs.prepare-matrix.outputs.manager_cert_sha256', kmi_expr, ddk_short_expr)
    return 'gki-sukisu-lkm-v2-' + '-'.join(expression(field) for field in fields)


def manager_lkm_job(condition: str) -> dict:
    cache_key = lkm_cache_key('matrix.kmi', 'matrix.ddk_short')
    cache_path = 'manager-lkm/${{ matrix.kmi }}'
    public_env = {
        'MANAGER_PACKAGE': '${{ needs.prepare-matrix.outputs.manager_package }}',
        'MANAGER_CERT_SIZE': '${{ needs.prepare-matrix.outputs.manager_cert_size }}',
        'MANAGER_CERT_SHA256': '${{ needs.prepare-matrix.outputs.manager_cert_sha256 }}',
    }
    build_env = {
        **public_env,
        'KMI': '${{ matrix.kmi }}', 'DDK_IMAGE': '${{ matrix.image }}',
        'KSU_REF': '${{ needs.prepare-matrix.outputs.ksu_ref }}',
    }
    return {
        'name': 'SukiSU 原生模块 / ${{ matrix.kmi }}',
        'needs': 'prepare-matrix', 'if': condition,
        'runs-on': 'ubuntu-24.04', 'permissions': {'contents': 'read'},
        'timeout-minutes': 30,
        'defaults': {'run': {'shell': 'bash'}},
        'strategy': {'fail-fast': False, 'max-parallel': 8,
                     'matrix': '${{ fromJSON(needs.prepare-matrix.outputs.ddk_matrix) }}'},
        'steps': [
            {'name': '检出构建工具', 'uses': 'actions/checkout@v6',
             'with': {'persist-credentials': False}},
            {'name': '下载本次固定的 SukiSU 源码', 'uses': 'actions/download-artifact@v8',
             'with': {'name': 'Source-SukiSU-${{ needs.prepare-matrix.outputs.ksu_short }}',
                      'path': 'ksu-source-bundle'}},
            {'name': '还原并核实 SukiSU 源码',
             'env': {'KSU_REF': '${{ needs.prepare-matrix.outputs.ksu_ref }}'},
             'run': '''set -euo pipefail
git config --global --add safe.directory "$GITHUB_WORKSPACE/manager-source"
python3 tools/checkout_ksu_snapshot.py --input ksu-source-bundle \
  --output manager-source --expected-ref "$KSU_REF"
'''},
            {'name': '核实管理器公开签名身份', 'env': public_env,
             'run': '''set -euo pipefail
python3 - <<'PY'
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, 'tools')
from prepare_manager_lkm import public_identity
path = Path('manager-public-certificate.json')
path.write_text(json.dumps({
    'manager_package': os.environ['MANAGER_PACKAGE'],
    'manager_cert_size': os.environ['MANAGER_CERT_SIZE'],
    'manager_cert_sha256': os.environ['MANAGER_CERT_SHA256'],
}, sort_keys=True) + '\\n', encoding='utf-8')
public_identity(path)
PY
'''},
            {'name': '还原匹配源码和签名的原生模块', 'id': 'lkm-cache',
             'uses': 'actions/cache/restore@v5', 'timeout-minutes': 3,
             'env': {'SEGMENT_DOWNLOAD_TIMEOUT_MINS': '2'},
             'with': {'path': cache_path, 'key': cache_key}},
            {'name': '核实已缓存的原生模块',
             'if': "steps.lkm-cache.outputs.cache-hit == 'true'", 'env': build_env,
             'run': '''set -euo pipefail
python3 tools/prepare_manager_lkm.py stage --source manager-source \
  --artifacts "manager-lkm/$KMI" --identity manager-public-certificate.json \
  --expected-ref "$KSU_REF" --expected-kmis "$KMI" \
  --report "$RUNNER_TEMP/manager-lkm-verified.json"
'''},
            {'name': '编译并核实同版本原生模块',
             'if': "steps.lkm-cache.outputs.cache-hit != 'true'", 'env': build_env,
             'run': '''set -euo pipefail
mkdir -p lkm-output
KSU_VERSION=$(python3 tools/prepare_manager_lkm.py version-code \
  --source manager-source --expected-ref "$KSU_REF")
docker pull "$DDK_IMAGE"
docker run --rm --network none \
  --volume "$GITHUB_WORKSPACE:$GITHUB_WORKSPACE" \
  --workdir "$GITHUB_WORKSPACE/manager-source/kernel" \
  --env HOME=/tmp --env KSU_PACKAGE_NAME="$MANAGER_PACKAGE" \
  --env KSU_EXPECTED_SIZE="$MANAGER_CERT_SIZE" \
  --env KSU_EXPECTED_HASH="$MANAGER_CERT_SHA256" --env KSU_VERSION="$KSU_VERSION" \
  --env MODULE_OUTPUT="$GITHUB_WORKSPACE/lkm-output/${KMI}_kernelsu.ko" \
  --entrypoint /bin/bash "$DDK_IMAGE" -euo pipefail -c '
    umask 022
    git config --global --add safe.directory "$(dirname "$PWD")"
    CONFIG_KSU=m CC=clang make -j "$(nproc)" \
      KSU_PACKAGE_NAME="$KSU_PACKAGE_NAME" KSU_EXPECTED_SIZE="$KSU_EXPECTED_SIZE" \
      KSU_EXPECTED_HASH="$KSU_EXPECTED_HASH" KSU_VERSION="$KSU_VERSION"
    cp kernelsu.ko "$MODULE_OUTPUT"
    llvm-strip -d "$MODULE_OUTPUT"
    chmod a+r "$MODULE_OUTPUT"
  '
python3 tools/prepare_manager_lkm.py record --source manager-source \
  --module "lkm-output/${KMI}_kernelsu.ko" --kmi "$KMI" \
  --identity manager-public-certificate.json --expected-ref "$KSU_REF" \
  --ddk-image "$DDK_IMAGE" --output "manager-lkm/$KMI"
'''},
            {'name': '保存已核实的原生模块供管理器使用',
             'if': "steps.lkm-cache.outputs.cache-hit != 'true'",
             'uses': 'actions/cache/save@v5', 'timeout-minutes': 3,
             'with': {'path': cache_path, 'key': cache_key}},
            {'name': '确认管理器可读取原生模块缓存',
             'uses': 'actions/cache/restore@v5', 'timeout-minutes': 3,
             'with': {'path': cache_path, 'key': cache_key,
                      'lookup-only': True, 'fail-on-cache-miss': True}},
        ],
    }

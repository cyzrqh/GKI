"""Bound one UI-only build to one frozen original release asset."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'tools'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('matrix', 'download', 'sign'))
    parser.add_argument('--asset-id', type=int)
    parser.add_argument('--pilot', action='store_true')
    parser.add_argument('--extra', action='store_true')
    args = parser.parse_args()
    rows = json.loads((HERE / 'matrix.json').read_bytes())['include']
    if args.operation == 'matrix':
        if args.extra:
            rows = [row for row in rows if row['release_id'] == 436153891]
            if len(rows) != 2:
                raise ValueError('Only the two newly included APKs may be rebuilt')
        if args.pilot:
            chosen = {}
            for row in reversed(rows):
                chosen.setdefault(row['variant'], row)
            rows = list(chosen.values())
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as stream:
            stream.write('matrix=' + json.dumps({'include': rows}, separators=(',', ':')) + '\n')
        return
    matches = [row for row in rows if row['asset_id'] == args.asset_id]
    if len(matches) != 1:
        raise ValueError('Asset is outside the frozen latest-ten-release scope')
    row = matches[0]
    original = ROOT / 'original' / str(args.asset_id) / row['name']
    if args.operation == 'download':
        original.parent.mkdir(parents=True, exist_ok=True)
        request = Request(row['url'], headers={'User-Agent': 'gki-release-ui-refresh'})
        temporary = original.with_suffix('.download')
        with urlopen(request, timeout=90) as response, temporary.open('wb') as output:
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 256 * 1024 * 1024:
                    raise ValueError('Original APK exceeds its size bound')
                output.write(chunk)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != row['old_sha256']:
            raise ValueError('Original APK changed since the scope was frozen')
        temporary.replace(original)
        return
    from sign_sukisu_manager import sign_manager
    import zipfile
    with zipfile.ZipFile(original) as archive:
        metadata = json.loads(archive.read('assets/gki-build.json'))
    os.environ.update(KSU_VARIANT=metadata['ksu_variant'], KSU_REF=metadata['ksu_ref'], KSU_BRANCH=metadata['ksu_branch'])
    output = ROOT / 'result' / str(args.asset_id)
    output.mkdir(parents=True, exist_ok=True)
    build = Path(os.environ['RUNNER_TEMP']) / 'manager-build'
    sign_manager(build / 'manager-unsigned.apk', output_dir=output,
                 build_metadata=build / 'manager-build-metadata.json')
    subprocess.run([sys.executable, str(HERE / 'verify_refresh.py'),
        '--original', str(original), '--updated', str(output / row['name']),
        '--output', str(output / 'receipt.json'), '--expected-source-ref', row['ksu_ref'],
        '--ui-head', os.environ['GITHUB_SHA'], '--tools', str(ROOT / 'tools'),
        '--sdk', os.environ['ANDROID_HOME']], check=True)


if __name__ == '__main__':
    main()

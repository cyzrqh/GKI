"""Merge the two approved runs by the frozen asset allowlist without overwrites."""
import json
from pathlib import Path
import shutil

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
matrix = json.loads((HERE / 'matrix.json').read_bytes())['include']
proof = json.loads((HERE / 'approved-builds.json').read_bytes())
destination = ROOT / 'rebuilt'
if destination.exists() or destination.is_symlink():
    raise ValueError('The merged artifact directory must not already exist')
destination.mkdir()
for row in matrix:
    new = row['release_id'] == 436153891
    origin = ROOT / ('extra-artifacts' if new else 'base-artifacts') / str(row['asset_id'])
    expected = proof['extra'] if new else proof['base']
    if (origin.is_symlink() or not origin.is_dir()
            or {p.name for p in origin.iterdir()} != {row['name'], 'receipt.json'}
            or any(not p.is_file() or p.is_symlink() for p in origin.iterdir())):
        raise ValueError('An artifact does not contain exactly its approved APK and receipt')
    receipt = json.loads((origin / 'receipt.json').read_bytes())
    if receipt['status'] != 'passed' or receipt['verified'] is not True or receipt['producer'] != {
            'repository': 'cyzrqh/GKI', **expected}:
        raise ValueError('The signed APK receipt does not match its approved producer run')
    if receipt['asset_name'] != row['name'] or receipt['old_apk_sha256'] != row['old_sha256']:
        raise ValueError('The artifact is not bound to its frozen original APK')
    shutil.copytree(origin, destination / str(row['asset_id']))
if len(list(destination.iterdir())) != 20:
    raise ValueError('Exactly twenty approved manager APK directories must be selected')
print('Selected 18 original build results and 2 newly built APKs; r1.13 is untouched.')

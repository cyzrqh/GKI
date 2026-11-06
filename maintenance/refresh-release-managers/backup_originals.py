"""Download and validate only original APK/checksum attachments for rollback."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
DESTINATION = Path(os.environ['RUNNER_TEMP']) / 'release-backup'
plan = json.loads((HERE / 'frozen-plan.json').read_bytes())


def backup(item):
    release, asset = item
    target = DESTINATION / str(release['id']) / asset['name']
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            request = Request(asset['browser_download_url'], headers={'User-Agent': 'gki-ui-refresh-backup'})
            with urlopen(request, timeout=90) as response:
                raw = response.read(asset['size'] + 1)
            if len(raw) != asset['size'] or 'sha256:' + hashlib.sha256(raw).hexdigest() != asset['digest']:
                raise ValueError('Original release attachment changed')
            target.write_bytes(raw)
            return
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(2)


items = []
for release in plan['releases']:
    ids = {row['asset_id'] for row in release['replacements']} | {release['checksum_asset_id']}
    items.extend((release, asset) for asset in release['assets'] if asset['id'] in ids)
if len(items) != 30:
    raise ValueError('Only the authorized 20 APKs and 10 checksum files may be backed up')
with ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(backup, items))
print('Verified rollback copies of all 30 original attachments.')

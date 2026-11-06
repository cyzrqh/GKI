"""Publish only a successful, complete build from this exact maintenance commit."""
import json
import os
from pathlib import Path
import re
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent


def api(path):
    request = Request('https://api.github.com/repos/cyzrqh/GKI' + path,
        headers={'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
                 'Accept': 'application/vnd.github+json', 'User-Agent': 'gki-ui-refresh-verify'})
    with urlopen(request, timeout=60) as response:
        return json.load(response)


run_id = os.environ['BUILD_RUN']
if not re.fullmatch(r'[1-9][0-9]{5,15}', run_id):
    raise ValueError('Provide the successful full maintenance build run')
run = api('/actions/runs/' + run_id)
if (run['status'] != 'completed' or run['conclusion'] != 'success'
        or run['head_sha'] != os.environ['GITHUB_SHA']
        or run['path'] != '.github/workflows/kernel-full-feature-matrix.yml'
        or run['display_title'] != '管理器界面维护 · build · 最新10个发行'):
    raise ValueError('The selected run is not this exact successful full APK-only build')
expected = {'manager-ui-' + str(row['asset_id']) for row in
            json.loads((ROOT / 'matrix.json').read_bytes())['include']}
artifacts = api('/actions/runs/' + run_id + '/artifacts?per_page=100')['artifacts']
if {row['name'] for row in artifacts if not row['expired']} != expected or len(artifacts) != 19:
    raise ValueError('The selected build does not have exactly 19 verified APK artifacts')
print('Verified the exact successful 19-APK maintenance build.')

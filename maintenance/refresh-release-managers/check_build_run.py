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


def checked_run(run_id, head, title, expected):
    if not re.fullmatch(r'[1-9][0-9]{5,15}', str(run_id)):
        raise ValueError('Provide an exact successful maintenance build run')
    run = api('/actions/runs/' + str(run_id))
    if (run['status'] != 'completed' or run['conclusion'] != 'success'
            or run['head_sha'] != head or run['run_attempt'] != 1
            or run['path'] != '.github/workflows/kernel-full-feature-matrix.yml'
            or run['display_title'] != title):
        raise ValueError('The selected run is not the approved successful APK-only build')
    artifacts = api('/actions/runs/' + str(run_id) + '/artifacts?per_page=100')['artifacts']
    if {row['name'] for row in artifacts if not row['expired']} != expected or len(artifacts) != len(expected):
        raise ValueError('The selected build artifact set differs from the approved APK list')
    return {'run_id': int(run_id), 'run_attempt': 1, 'head_sha': head}


matrix = json.loads((ROOT / 'matrix.json').read_bytes())['include']
base_matrix = json.loads((ROOT / 'base-matrix.json').read_bytes())['include']
base_head = 'ff9fa76d1cf5fd9783e1b9035ab7100b64f6ee4a'
base = checked_run(42175482438, base_head, '管理器界面维护 · build · 最新10个发行',
                   {'manager-ui-' + str(row['asset_id']) for row in base_matrix})
extra_ids = {row['asset_id'] for row in matrix if row['release_id'] == 436153891}
if len(extra_ids) != 2 or len(matrix) != 20:
    raise ValueError('The frozen scope must contain exactly 20 APKs with two new r2.2 APKs')
extra = checked_run(os.environ['BUILD_RUN'], os.environ['GITHUB_SHA'],
                    '管理器界面维护 · extra · 最新10个发行',
                    {'manager-ui-' + str(asset_id) for asset_id in extra_ids})
document = {'schema_version': 1, 'base': base, 'extra': extra,
            'selected_asset_ids': sorted(row['asset_id'] for row in matrix)}
(ROOT / 'approved-builds.json').write_text(json.dumps(document) + '\n', encoding='utf-8')
print('Verified the exact successful base build and the two additional APKs.')

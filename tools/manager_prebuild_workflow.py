"""Generate manual backfill and add official APK prebuilds to the existing check."""
from __future__ import annotations

from pathlib import Path


OUTPUT = Path('.github/workflows/prebuild-manager-apks.yml')
WORKER = './.github/workflows/_manager-apk-worker.yml'
STATE_FILE = '${{ runner.temp }}/gki-manager-apk-observations.json'
STATE_PREFIX = 'gki-manager-apk-observations-v2-'
STATE_KEY = STATE_PREFIX + '${{ github.run_id }}-${{ github.run_attempt }}'
DEFAULT_BRANCH = "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
DISCOVERY_OUTPUTS = ('matrix', 'pending', 'state', 'state_changed')


def build_job(discover: str, *, prefix: str = '') -> dict:
    outputs = 'needs.' + discover + '.outputs.' + prefix
    return {
        'name': '${{ matrix.variant }} · ${{ matrix.channels }} APK',
        'if': f"always() && !cancelled() && {outputs}pending == 'true'",
        'needs': [discover],
        'permissions': {'contents': 'read', 'actions': 'write'},
        'strategy': {'fail-fast': False, 'max-parallel': 3,
                     'matrix': '${{ fromJSON(' + outputs + 'matrix) }}'},
        'uses': WORKER,
        'with': {key: '${{ matrix.' + key + ' }}'
                 for key in ('variant', 'tier', 'reference', 'source_branch')},
        'secrets': 'inherit',
    }


def record_job(discover: str, *, prefix: str = '') -> dict:
    outputs = 'needs.' + discover + '.outputs.' + prefix
    # A removed channel needs no build, but saving its absence lets a restored
    # release be detected later. A failed worker can never advance the state.
    recorded = ("needs.prebuild-managers.result == 'success' || "
                "(needs.prebuild-managers.result == 'skipped' && "
                f"{outputs}pending == 'false' && {outputs}state_changed == 'true')")
    return {
        'name': '记录已验证的官方版本状态',
        'needs': [discover, 'prebuild-managers'],
        'if': f"always() && !cancelled() && ({recorded})",
        'runs-on': 'ubuntu-24.04',
        'timeout-minutes': 4,
        'permissions': {'contents': 'read', 'actions': 'write'},
        'steps': [
            {'name': '检出验证工具', 'uses': 'actions/checkout@v6',
             'with': {'persist-credentials': False}},
            {'name': '验证已完成的官方版本',
             'env': {'STATE_JSON': '${{ ' + outputs + 'state }}'},
             'run': 'python3 tools/manager_prebuild.py record --state-json "$STATE_JSON" '
                    '--output "$RUNNER_TEMP/gki-manager-apk-observations.json"'},
            {'name': '保存成功版本记录', 'uses': 'actions/cache/save@v5',
             'with': {'path': STATE_FILE, 'key': STATE_KEY}},
        ],
    }


def create_workflow() -> dict:
    return {
        'name': '预编译官方管理器 APK',
        'on': {'workflow_dispatch': {}},
        'permissions': {'contents': 'read'},
        'jobs': {
            'discover': {
                'name': '确定六种管理器的正式版与最新版',
                'if': DEFAULT_BRANCH,
                'runs-on': 'ubuntu-24.04',
                'timeout-minutes': 8,
                'outputs': {key: '${{ steps.sources.outputs.' + key + ' }}'
                            for key in DISCOVERY_OUTPUTS},
                'steps': [
                    {'name': '检出验证工具', 'uses': 'actions/checkout@v6',
                     'with': {'persist-credentials': False}},
                    {'name': '选出可构建的官方提交', 'id': 'sources',
                     'env': {'GH_TOKEN': '${{ github.token }}'},
                     'run': 'python3 tools/manager_prebuild.py discover'},
                ],
            },
            'prebuild-managers': build_job('discover'),
            'record-manager-prebuild': record_job('discover'),
        },
    }


def add_updater_call(workflow: dict) -> dict:
    jobs = workflow['jobs']
    check = jobs['check-upstream']
    steps = check['steps']
    official = next(index for index, step in enumerate(steps) if step.get('id') == 'official')
    condition = "steps.official.outcome == 'success' || steps.official.outcome == 'failure'"
    steps[official + 1:official + 1] = [
        {'name': '恢复已完成的官方 APK 版本记录', 'if': condition,
         'uses': 'actions/cache/restore@v5', 'continue-on-error': True, 'timeout-minutes': 2,
         'with': {'path': STATE_FILE, 'key': STATE_KEY, 'restore-keys': STATE_PREFIX}},
        {'name': '选择需要预编译的官方 APK 更新', 'id': 'manager-prebuild', 'if': condition,
         'continue-on-error': True,
         'env': {'GH_TOKEN': '${{ github.token }}', 'OFFICIAL_RESULT': '${{ steps.official.outcome }}',
                 'OFFICIAL_CANDIDATES': '${{ steps.official.outputs.candidates_json }}'},
         'run': '''set -euo pipefail
args=(--previous "$RUNNER_TEMP/gki-manager-apk-observations.json")
if [ "$OFFICIAL_RESULT" = success ] && [ -n "$OFFICIAL_CANDIDATES" ]; then
  args+=(--candidates-json "$OFFICIAL_CANDIDATES")
else
  echo '综合官方源检查未提供完整结果，直接核对六种管理器的官方版本。'
fi
python3 tools/manager_prebuild.py discover "${args[@]}"
'''},
    ]
    check.setdefault('outputs', {}).update({
        'manager_prebuild_' + key: '${{ steps.manager-prebuild.outputs.' + key + ' }}'
        for key in DISCOVERY_OUTPUTS
    })
    jobs['prebuild-managers'] = build_job('check-upstream', prefix='manager_prebuild_')
    jobs['record-manager-prebuild'] = record_job('check-upstream', prefix='manager_prebuild_')
    return workflow

"""Build official APKs from the existing schedule without a manual trigger."""
from __future__ import annotations

OUTPUT = '.github/workflows/prebuild-manager-apks.yml'
STATE_FILE = '${{ runner.temp }}/gki-manager-apk-observations.json'
STATE_PREFIX = 'gki-manager-apk-observations-v2-'
STATE_KEY = STATE_PREFIX + '${{ github.run_id }}-${{ github.run_attempt }}'
AUTOMATIC = "(github.event_name == 'schedule' || github.event_name == 'push')"
DEFAULT = "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
CHECK_OUTPUTS = 'needs.check-upstream.outputs.manager_prebuild_'
DISCOVERY_OUTPUTS = ('matrix', 'lkm_matrix', 'pending', 'state', 'state_changed')


def record_job() -> dict:
    # A removed channel needs no build, but saving its absence lets a restored
    # release be detected later. A failed worker can never advance the state.
    recorded = ("needs.prebuild-manager-apks.result == 'success' || "
                "(needs.prebuild-manager-apks.result == 'skipped' && "
                f"{CHECK_OUTPUTS}pending == 'false' && {CHECK_OUTPUTS}state_changed == 'true')")
    return {
        'name': '记录已验证的官方版本状态',
        'needs': ['check-upstream', 'prebuild-manager-apks'],
        'if': f"always() && !cancelled() && ({recorded})",
        'runs-on': 'ubuntu-24.04',
        'timeout-minutes': 4,
        'permissions': {'contents': 'read', 'actions': 'write'},
        'steps': [
            {'name': '检出验证工具', 'uses': 'actions/checkout@v6',
             'with': {'persist-credentials': False}},
            {'name': '验证已完成的官方版本',
             'env': {'STATE_JSON': '${{ ' + CHECK_OUTPUTS + 'state }}'},
             'run': 'python3 tools/manager_prebuild.py record --state-json "$STATE_JSON" '
                    '--output "$RUNNER_TEMP/gki-manager-apk-observations.json"'},
            {'name': '保存成功版本记录', 'uses': 'actions/cache/save@v5',
             'with': {'path': STATE_FILE, 'key': STATE_KEY}},
        ],
    }


def prebuild_workflow(base: dict) -> dict:
    from manager_apk_worker import create_jobs

    discovery = {
        'name': '确定六种管理器的正式版与最新版',
        'if': AUTOMATIC + ' && ' + DEFAULT,
        'runs-on': 'ubuntu-24.04', 'timeout-minutes': 10,
        'outputs': {'manager_prebuild_' + key: '${{ steps.manager-prebuild.outputs.' + key + ' }}'
                    for key in DISCOVERY_OUTPUTS},
        'steps': [
        {'name': '检出版本发现工具', 'uses': 'actions/checkout@v6',
         'with': {'persist-credentials': False}},
        {'name': '恢复已完成的官方 APK 版本记录',
         'uses': 'actions/cache/restore@v5', 'continue-on-error': True, 'timeout-minutes': 2,
         'with': {'path': STATE_FILE, 'key': STATE_KEY, 'restore-keys': STATE_PREFIX}},
        {'name': '选择需要预编译的官方 APK 更新', 'id': 'manager-prebuild',
         'env': {'GH_TOKEN': '${{ github.token }}'},
         'run': 'python3 tools/manager_prebuild.py discover '
                '--previous "$RUNNER_TEMP/gki-manager-apk-observations.json"'},
        ],
    }
    return {'name': '自动预编译管理器 APK', 'on': {'workflow_call': {}},
            'permissions': {'contents': 'read', 'actions': 'write'},
            'jobs': {'check-upstream': discovery, **create_jobs(base), 'record-manager-prebuild': record_job()}}


def add_updater_call(workflow: dict) -> dict:
    # The initial push of the APK workflow starts backfill immediately. Further
    # checks use the existing cron, with no standalone APK run or button.
    workflow['on']['push'] = {'branches': ['main'], 'paths': [OUTPUT]}
    workflow['jobs']['prebuild-managers'] = {
        'name': '自动预编译管理器 APK', 'if': AUTOMATIC + ' && ' + DEFAULT,
        'uses': './' + OUTPUT, 'permissions': {'contents': 'read', 'actions': 'write'}, 'secrets': 'inherit',
    }
    return workflow

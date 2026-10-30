"""Build, update, release, and standalone history-cleanup workflow jobs."""
import re


HISTORY_CLEANUP_NAME = '清理历史记录（保留2条成功及发行记录）'


def successful_cleanup_sweep_step():
    return {
        'name': '自动删除已成功的清理记录',
        'if': 'always() && !cancelled()',
        'continue-on-error': True, 'timeout-minutes': 2,
        'env': {'GH_TOKEN': '${{ github.token }}'},
        'run': 'python3 tools/remove_successful_cleanup_runs.py --repo "$GITHUB_REPOSITORY" --execute',
    }


def cleanup_history_job():
    return {
        'name': HISTORY_CLEANUP_NAME,
        'if': "github.event_name == 'workflow_dispatch' && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)",
        'runs-on': 'ubuntu-24.04', 'timeout-minutes': 15,
        'permissions': {'contents': 'read', 'actions': 'write'},
        'concurrency': {'group': '${{ github.repository }}-history-cleanup', 'cancel-in-progress': False},
        'steps': [
            {'name': '检出默认分支', 'uses': 'actions/checkout@v6',
             'with': {'ref': '${{ github.event.repository.default_branch }}', 'persist-credentials': False}},
            {'name': '清理旧 Actions 记录及其产物（发行记录和发行版全部保留）',
             'env': {'GH_TOKEN': '${{ github.token }}'},
             'run': '''python3 tools/cleanup_workflow_runs.py \\
  --repo "$GITHUB_REPOSITORY" --workflow all \\
  --keep 2 --exclude-workflow cleanup-build-history.yml --execute --summary "$GITHUB_STEP_SUMMARY"
'''},
        ],
    }


def cleanup_history_workflow():
    return {
        'name': HISTORY_CLEANUP_NAME,
        'run-name': HISTORY_CLEANUP_NAME,
        'on': {'workflow_dispatch': {}},
        'permissions': {'contents': 'read'},
        'jobs': {'cleanup-history': cleanup_history_job()},
    }


def updater_job():
    return {
        'name': '检查上游并按选择手动同步',
        'if': "(github.event_name == 'workflow_dispatch' || github.event_name == 'schedule') && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)",
        'runs-on': 'ubuntu-24.04', 'timeout-minutes': 20,
        'permissions': {'contents': 'read', 'pull-requests': 'write', 'actions': 'write'},
        'concurrency': {'group': '${{ github.repository }}-official-sync', 'cancel-in-progress': False},
        'steps': [
            {'name': '检出默认分支', 'uses': 'actions/checkout@v6',
             'with': {'ref': '${{ github.event.repository.default_branch }}', 'fetch-depth': 0, 'persist-credentials': False}},
            {'name': '清理上一批已结束的更新检查记录', 'id': 'previous-checks',
             'if': "github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && inputs.operation == '仅检查并提醒')",
             'continue-on-error': True, 'timeout-minutes': 2,
             'env': {'GH_TOKEN': '${{ github.token }}'},
             'run': 'python3 tools/remove_previous_update_checks.py --repo "$GITHUB_REPOSITORY" --execute'},
            {'name': '恢复已验证的官方内核版本快照', 'uses': 'actions/cache/restore@v5',
             'with': {'path': '${{ runner.temp }}/gki-kernel-catalog',
                      'key': 'gki-official-catalog-v1-${{ github.run_id }}-${{ github.run_attempt }}',
                      'restore-keys': 'gki-official-catalog-v1-'}},
            {'name': '检查 SukiSU、BakaSU 与 SUSFS 官方更新', 'id': 'official',
             'continue-on-error': True,
             'env': {'GH_TOKEN': '${{ github.token }}'},
             'run': 'python3 tools/check_official_updates.py --check-only'},
            {'name': '检查 Android 官方 GKI 版本', 'id': 'catalog',
             'continue-on-error': True,
             'run': 'python3 tools/update_kernel_catalog.py --check --snapshot-file "$RUNNER_TEMP/gki-kernel-catalog/refs.json" --github-output "$GITHUB_OUTPUT"'},
            {'name': '保存已验证的官方内核版本快照', 'uses': 'actions/cache/save@v5',
             'if': 'always() && !cancelled()', 'continue-on-error': True,
             'with': {'path': '${{ runner.temp }}/gki-kernel-catalog',
                      'key': 'gki-official-catalog-v1-${{ github.run_id }}-${{ github.run_attempt }}'}},
            {'name': '汇总官方更新', 'id': 'probe',
             'env': {'OFFICIAL_CHANGED': '${{ steps.official.outputs.has_updates }}',
                     'CATALOG_CHANGED': '${{ steps.catalog.outputs.has_updates }}',
                     'OFFICIAL_CANDIDATES': '${{ steps.official.outputs.candidates_json }}',
                     'CATALOG_CANDIDATES': '${{ steps.catalog.outputs.candidates_json }}',
                     'OFFICIAL_RESULT': '${{ steps.official.outcome }}',
                     'CATALOG_RESULT': '${{ steps.catalog.outcome }}'},
             'run': '''python3 - <<'PY'
import hashlib,json,os
official_ok = os.environ['OFFICIAL_RESULT'] == 'success'
catalog_ok = os.environ['CATALOG_RESULT'] == 'success'
changed = ((official_ok and os.environ['OFFICIAL_CHANGED'] == 'true') or
           (catalog_ok and os.environ['CATALOG_CHANGED'] == 'true'))
snapshot = [json.loads(os.environ['OFFICIAL_CANDIDATES']) if official_ok else None,
            json.loads(os.environ['CATALOG_CANDIDATES']) if catalog_ok else [],
            {'official_ok': official_ok, 'catalog_ok': catalog_ok}]
digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
    output.write(f"has_updates={str(changed).lower()}\\nheads_marker=<!-- gki-official-heads:sha256={digest} -->\\n")
    output.write(f"partial_failure={str(not (official_ok and catalog_ok)).lower()}\\n")
PY
'''},
            {'name': '恢复管理器版本号的官方 Git 历史', 'uses': 'actions/cache/restore@v5',
             'if': "steps.probe.outputs.has_updates == 'true'", 'continue-on-error': True,
             'with': {'path': '${{ runner.temp }}/gki-manager-version-history',
                      'key': 'gki-manager-version-history-v1-${{ github.run_id }}-${{ github.run_attempt }}',
                      'restore-keys': 'gki-manager-version-history-v1-'}},
            {'name': '检查更新分支归属和已有 PR', 'id': 'guard',
             'if': "steps.probe.outputs.has_updates == 'true'",
             'env': {'GH_TOKEN': '${{ github.token }}', 'HEADS_MARKER': '${{ steps.probe.outputs.heads_marker }}',
                     'DEFAULT_BRANCH': '${{ github.event.repository.default_branch }}'},
             'run': 'python3 tools/upstream_pr_guard.py --heads-marker "$HEADS_MARKER" --base "$DEFAULT_BRANCH" --github-output "$GITHUB_OUTPUT"'},
            {'name': '准备独立官方更新', 'id': 'update',
             'if': "steps.probe.outputs.has_updates == 'true'",
             'env': {'OFFICIAL_CANDIDATES': '${{ steps.official.outputs.candidates_json }}',
                     'CATALOG_CANDIDATES': '${{ steps.catalog.outputs.candidates_json }}',
                     'HEADS_MARKER': '${{ steps.probe.outputs.heads_marker }}',
                     'OFFICIAL_RESULT': '${{ steps.official.outcome }}',
                     'CATALOG_RESULT': '${{ steps.catalog.outcome }}'},
             'run': '''set -euo pipefail
if [ "$OFFICIAL_RESULT" = success ]; then
  python3 tools/check_official_updates.py --prepare --candidates-json "$OFFICIAL_CANDIDATES" --report-dir "$RUNNER_TEMP"
fi
if [ "$CATALOG_RESULT" = success ]; then
  python3 tools/update_kernel_catalog.py --apply-candidates-json "$CATALOG_CANDIDATES"
fi
python3 tools/official_sync_report.py --root . --base-ref HEAD \\
  --report-json "$RUNNER_TEMP/official-sync-report.json" \\
  --report-markdown "$RUNNER_TEMP/official-sync-report.md" \\
  --observe-manager-versions --manager-cache "$RUNNER_TEMP/gki-manager-version-history" \\
  --official-result "$OFFICIAL_RESULT" --catalog-result "$CATALOG_RESULT"
python3 - <<'PY'
import json,os,pathlib
path = pathlib.Path(os.environ['RUNNER_TEMP']) / 'official-update.md'
official_ok = os.environ['OFFICIAL_RESULT'] == 'success'
catalog_ok = os.environ['CATALOG_RESULT'] == 'success'
status = ('SukiSU/BakaSU/SUSFS：' + ('检查完成' if official_ok else '检查失败，未确认有无更新') +
          '；Android GKI：' + ('检查完成' if catalog_ok else '检查失败，未确认有无更新'))
if not (official_ok and catalog_ok):
    status += '。本次仅提交成功检查部分的候选；失败部分须重试，不能视为全部官方源已检查。'
body = ('<!-- gki-upstream-sync:v1 -->\\n' + os.environ['HEADS_MARKER'] + '\\n\\n'
        '# 官方源更新\\n\\n本 PR 直接跟踪 SukiSU、BakaSU、SUSFS 和 Android GKI 官方源，不依赖其他构建仓库。\\n\\n'
        + status + '\\n\\n'
        '本 PR 只准备待同步的数据，不会因检查而自动合并。确认后可在 Actions 的“检查并同步官方更新”中选择“同步官方更新”，或手动合并此 PR。新 APK 和内核仍需另外触发构建。\\n\\n'
        + (path.parent / 'official-sync-report.md').read_text(encoding='utf-8'))
path.write_text(body,encoding='utf-8')
with open(os.environ['GITHUB_OUTPUT'],'a') as output:
    output.write(f'has_updates=true\\nbody_path={path}\\ntitle=Update official sources and GKI kernel catalog\\n')
PY
'''},
            {'name': '缓存管理器版本号的官方 Git 历史', 'uses': 'actions/cache/save@v5',
             'if': "always() && !cancelled() && steps.update.outcome == 'success'", 'continue-on-error': True,
             'with': {'path': '${{ runner.temp }}/gki-manager-version-history',
                      'key': 'gki-manager-version-history-v1-${{ github.run_id }}-${{ github.run_attempt }}'}},
            {'name': '检查更新令牌', 'if': "steps.update.outputs.has_updates == 'true'",
             'env': {'SYNC_TOKEN': '${{ secrets.UPSTREAM_SYNC_TOKEN }}'},
             'run': 'test -n "$SYNC_TOKEN" || { echo "::error::请配置 UPSTREAM_SYNC_TOKEN 以自动同步官方更新"; exit 1; }'},
            {'name': '创建或更新官方源 PR', 'id': 'pr', 'if': "steps.update.outputs.has_updates == 'true' && steps.guard.outputs.already_pending != 'true'",
             'uses': 'peter-evans/create-pull-request@v8',
             'with': {'token': '${{ secrets.UPSTREAM_SYNC_TOKEN }}', 'branch': 'automation/upstream-sync',
                      'base': '${{ github.event.repository.default_branch }}',
                      'title': '${{ steps.update.outputs.title }}', 'body-path': '${{ steps.update.outputs.body_path }}',
                      'commit-message': 'Track official source updates and new GKI kernels',
                      'add-paths': 'data/kernels.json\ndata/upstream-candidates.json\n'}},
            {'name': '校验并执行手动同步', 'id': 'sync',
             'if': "github.event_name == 'workflow_dispatch' && inputs.operation == '同步官方更新' && steps.probe.outputs.has_updates == 'true' && (steps.pr.outputs.pull-request-number != '' || steps.guard.outputs.already_pending == 'true')",
             'env': {'GH_TOKEN': '${{ secrets.UPSTREAM_SYNC_TOKEN }}',
                     'CREATED_PR_NUMBER': '${{ steps.pr.outputs.pull-request-number }}',
                     'EXISTING_PR_URL': '${{ steps.guard.outputs.pull_request_url }}',
                     'DEFAULT_BRANCH': '${{ github.event.repository.default_branch }}'},
             'run': '''set -euo pipefail
number="${CREATED_PR_NUMBER:-${EXISTING_PR_URL##*/}}"
python3 tools/auto_merge_official_update.py --pull-request "$number" --base "$DEFAULT_BRANCH"
'''},
            {'name': '仅发送待手动同步的更新提醒', 'id': 'notify',
             'if': "always() && !cancelled() && steps.update.outcome == 'success' && steps.sync.outputs.merged != 'true' && (steps.pr.outputs.pull-request-number != '' || steps.guard.outputs.already_pending == 'true')",
             'env': {'GH_TOKEN': '${{ github.token }}',
                     'PR_REFERENCE': '${{ steps.pr.outputs.pull-request-number || steps.guard.outputs.pull_request_url }}',
                     'NOTICE_STATE': 'pending',
                     'DEFAULT_BRANCH': '${{ github.event.repository.default_branch }}'},
             'run': '''python3 tools/notify_official_sync.py --report "$RUNNER_TEMP/official-sync-report.json" \\
  --pull-request "$PR_REFERENCE" --state "$NOTICE_STATE" --base "$DEFAULT_BRANCH"
'''},
            {'name': '记录检查结果',
             'if': 'always() && !cancelled()',
             'env': {'PR_URL': '${{ steps.sync.outputs.pull_request_url || steps.pr.outputs.pull-request-url }}',
                     'SYNC_MERGED': '${{ steps.sync.outputs.merged }}',
                     'SYNC_COMMIT': '${{ steps.sync.outputs.merge_commit }}',
                     'HAS_UPDATES': '${{ steps.probe.outputs.has_updates }}',
                     'EXISTING_PR': '${{ steps.guard.outputs.pull_request_url }}',
                     'OFFICIAL_RESULT': '${{ steps.official.outcome }}',
                     'CATALOG_RESULT': '${{ steps.catalog.outcome }}'},
             'run': '''if [ "$SYNC_MERGED" = true ]; then
  printf '你手动触发的官方更新已同步：%s（提交 %s）。本次没有编译。\\n' "$PR_URL" "$SYNC_COMMIT" >> "$GITHUB_STEP_SUMMARY"
elif [ -n "$PR_URL" ]; then
  printf '已发现官方更新，等待你手动同步：%s\\n' "$PR_URL" >> "$GITHUB_STEP_SUMMARY"
elif [ -n "$EXISTING_PR" ]; then
  printf '官方更新仍待同步，请查看校验或合并步骤：%s\\n' "$EXISTING_PR" >> "$GITHUB_STEP_SUMMARY"
elif [ "$OFFICIAL_RESULT" = success ] && [ "$CATALOG_RESULT" = success ] && [ "$HAS_UPDATES" = false ]; then
  echo '未发现新的官方源或 GKI 版本。' >> "$GITHUB_STEP_SUMMARY"
elif [ "$HAS_UPDATES" = true ]; then
  echo '已发现官方更新；请查看待同步 PR，或检查分支归属、令牌及 PR 步骤。' >> "$GITHUB_STEP_SUMMARY"
fi
if [ -f "$RUNNER_TEMP/official-sync-report.md" ]; then
  printf '\\n' >> "$GITHUB_STEP_SUMMARY"
  cat "$RUNNER_TEMP/official-sync-report.md" >> "$GITHUB_STEP_SUMMARY"
fi
if [ "$OFFICIAL_RESULT" != success ] || [ "$CATALOG_RESULT" != success ]; then
  printf '\\n部分官方检查未完成：SukiSU/BakaSU/SUSFS=%s，Android GKI=%s。失败部分须重试；不能确认全部官方源是否更新。\\n' "$OFFICIAL_RESULT" "$CATALOG_RESULT" >> "$GITHUB_STEP_SUMMARY"
fi
'''},
            successful_cleanup_sweep_step(),
            {'name': '提示未完成的官方检查',
             'if': "always() && !cancelled() && (steps.official.outcome == 'failure' || steps.catalog.outcome == 'failure')",
             'run': '''echo '::error::部分官方源检查失败；请查看失败步骤并重试。仅成功检查部分可以准备待同步数据。'
exit 1
'''},
        ],
    }


def updater_workflow():
    job = updater_job()
    return {
        'name': '检查并同步官方更新',
        'run-name': "${{ inputs.operation || '检查上游并提醒' }}",
        'on': {'schedule': [{'cron': '13 */3 * * *'}], 'workflow_dispatch': {'inputs': {
            'operation': {'description': '检查只准备更新并提醒；同步才会合并',
                          'required': True, 'type': 'choice',
                          'options': ['仅检查并提醒', '同步官方更新'], 'default': '仅检查并提醒'}}}},
        'permissions': {'contents': 'read'},
        'jobs': {'check-upstream': job},
    }


def release_publish_steps(condition: str, assets_directory='release-publish/assets',
                          notes_file='release-publish/release.md', title_file='release-publish/title.txt'):
    """Publish only caller-verified native assets under the shared run rules."""
    if not isinstance(condition, str) or not condition:
        raise ValueError('A release publish condition is required')
    for path in (assets_directory, notes_file, title_file):
        if (not isinstance(path, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_./-]*', path)
                or path.startswith('/') or any(part in ('', '.', '..') for part in path.split('/'))):
            raise ValueError('Release publishing requires safe relative workspace paths')
    publish = '''set -euo pipefail
title="$(cat release-publish/title.txt)"
if [ "$RELEASE_EXISTS" != true ]; then
gh release create "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" --target "$GITHUB_SHA" \\
  --draft --title "$title" --notes-file release-publish/release.md
fi
assets=(release-publish/assets/*)
gh release upload "$RELEASE_TAG" "${assets[@]}" --clobber --repo "$GITHUB_REPOSITORY"
gh release edit "$RELEASE_TAG" --draft=false --latest --title "$title" \\
  --notes-file release-publish/release.md --repo "$GITHUB_REPOSITORY"
url="https://github.com/$GITHUB_REPOSITORY/releases/tag/$RELEASE_TAG"
printf 'release_tag=%s\\n' "$RELEASE_TAG" >> "$GITHUB_OUTPUT"
printf 'release_url=%s\\n' "$url" >> "$GITHUB_OUTPUT"
printf '直接下载 APK、AK3 及详细构建配置：[打开 Release](%s)\\n' "$url" >> "$GITHUB_STEP_SUMMARY"
'''
    publish = (publish.replace('release-publish/assets', assets_directory)
               .replace('release-publish/release.md', notes_file)
               .replace('release-publish/title.txt', title_file))
    return [
        {'name': '分配递增发行标签并绑定本次构建', 'id': 'release-tag', 'if': condition,
         'env': {'GH_TOKEN': '${{ secrets.UPSTREAM_SYNC_TOKEN || github.token }}'},
         'run': 'python3 tools/release_tags.py allocate --notes ' + notes_file},
        {'name': '发布独立安装包和刷机包', 'id': 'release', 'if': condition,
         'env': {'GH_TOKEN': '${{ secrets.UPSTREAM_SYNC_TOKEN || github.token }}',
                 'RELEASE_TAG': '${{ steps.release-tag.outputs.release_tag }}',
                 'RELEASE_EXISTS': '${{ steps.release-tag.outputs.release_exists }}'}, 'run': publish},
        {'name': '核对原文件并清理发布中转包', 'if': "steps.release.outcome == 'success'",
         'env': {'GH_TOKEN': '${{ github.token }}', 'RELEASE_TAG': '${{ steps.release.outputs.release_tag }}'},
         'run': 'python3 tools/cleanup_build_artifacts.py --run-id "$GITHUB_RUN_ID" --release-tag "$RELEASE_TAG"'},
    ]


def release_job():
    condition = ("always() && !cancelled() && github.event_name == 'workflow_dispatch' && "
                 "needs.prepare-matrix.result == 'success' && "
                 "((inputs.operation == '编译内核' && needs.build-kernel.result == 'success' && "
                 "((inputs.kernelsu_variant != 'SukiSU' && inputs.kernelsu_variant != 'BakaSU') || needs.build-sukisu-manager.result == 'success')) || "
                 "(inputs.operation == '仅编译管理器安装包' && needs.build-sukisu-manager.result == 'success'))")
    return {
        'name': '整理产物与校验（全量成功才发行）', 'if': condition,
        'env': {'KSU_VARIANT': '${{ inputs.kernelsu_variant }}'},
        'needs': ['prepare-matrix', 'build-sukisu-manager', 'build-kernel'],
        'runs-on': 'ubuntu-24.04', 'timeout-minutes': 15,
        'permissions': {'contents': 'write', 'actions': 'write'},
        'steps': [
            {'name': '检出发布工具', 'uses': 'actions/checkout@v6', 'with': {'persist-credentials': False}},
            {'name': '准备 APK 内容签名验证',
             'run': "python3 -c 'import cryptography' || python3 -m pip install --break-system-packages --disable-pip-version-check cryptography"},
            {'name': '收集本次构建产物', 'uses': 'actions/download-artifact@v8',
             'with': {'pattern': "{${{ needs.build-sukisu-manager.outputs.apk_filename || '__NO_MANAGER_APK__' }},*-AnyKernel3.zip,*-boot*.img}",
                      'path': 'release-input', 'merge-multiple': False, 'skip-decompress': True}},
            {'name': '整理直接下载文件与实际配置', 'id': 'assets',
             'env': {'EXPECTED_KERNEL_COUNT': "${{ inputs.operation == '仅编译管理器安装包' && '0' || needs.prepare-matrix.outputs.count }}",
                     'REPORT_KIND': "${{ inputs.operation == '编译内核' && needs.prepare-matrix.outputs.full_catalog == 'true' && 'release' || 'actions' }}"},
             'run': 'python3 tools/prepare_release.py --input release-input --output release-publish --expected-kernel-count "$EXPECTED_KERNEL_COUNT" --report-kind "$REPORT_KIND"'},
            {'name': '上传同名校验文件', 'uses': 'actions/upload-artifact@v7',
             'with': {'path': 'release-publish/assets/SHA256SUMS.txt', 'archive': False,
                      'if-no-files-found': 'error', 'overwrite': True}},
            *release_publish_steps("inputs.operation == '编译内核' && needs.prepare-matrix.outputs.full_catalog == 'true'"),
            {'name': '显示构建配置与发行结果', 'if': 'always() && !cancelled()',
             'env': {'ASSETS_RESULT': '${{ steps.assets.outcome }}',
                     'RELEASE_RESULT': '${{ steps.release.outcome }}',
                     'FULL_CATALOG': '${{ needs.prepare-matrix.outputs.full_catalog }}',
                     'OPERATION': '${{ inputs.operation }}'},
             'run': '''if [ "$ASSETS_RESULT" = success ]; then
  if [ "$RELEASE_RESULT" = success ]; then
    echo '完整内核目录全部构建成功，已发布 Release。' >> "$GITHUB_STEP_SUMMARY"
  elif [ "$OPERATION" = '仅编译管理器安装包' ]; then
    echo '本次仅构建 APK，安装包和校验文件在 Actions 下载。' >> "$GITHUB_STEP_SUMMARY"
  elif [ "$FULL_CATALOG" != true ]; then
    echo '本次为单版本或部分版本构建，文件和校验信息在 Actions 下载；完整目录全部成功才发布 Release。' >> "$GITHUB_STEP_SUMMARY"
  else
    echo '完整目录构建文件已核验，发行步骤未完成，请查看相应日志。' >> "$GITHUB_STEP_SUMMARY"
  fi
  printf '\\n' >> "$GITHUB_STEP_SUMMARY"
  cat release-publish/release.md >> "$GITHUB_STEP_SUMMARY"
else
  echo '构建产物核验未完成，请查看整理步骤的日志。' >> "$GITHUB_STEP_SUMMARY"
fi
'''},
        ],
    }

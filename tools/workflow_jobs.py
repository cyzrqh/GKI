"""Official-source update and public release jobs for the single GKI workflow."""


def updater_job():
    return {
        'name': '检查官方源并准备更新 PR',
        'if': "(github.event_name == 'schedule' || inputs.operation == '检查上游更新') && github.ref_name == github.event.repository.default_branch",
        'runs-on': 'ubuntu-24.04', 'timeout-minutes': 20,
        'permissions': {'contents': 'read', 'pull-requests': 'write'},
        'concurrency': {'group': '${{ github.repository }}-official-sync', 'cancel-in-progress': False},
        'steps': [
            {'name': '检出默认分支', 'uses': 'actions/checkout@v6',
             'with': {'ref': '${{ github.event.repository.default_branch }}', 'fetch-depth': 0, 'persist-credentials': False}},
            {'name': '恢复已验证的官方内核版本快照', 'uses': 'actions/cache/restore@v5',
             'with': {'path': '${{ runner.temp }}/gki-kernel-catalog',
                      'key': 'gki-official-catalog-v1-${{ github.run_id }}-${{ github.run_attempt }}',
                      'restore-keys': 'gki-official-catalog-v1-'}},
            {'name': '检查 SukiSU 与 SUSFS 官方更新', 'id': 'official',
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
            {'name': '检查更新分支归属和已有 PR', 'id': 'guard',
             'if': "steps.probe.outputs.has_updates == 'true'",
             'env': {'GH_TOKEN': '${{ github.token }}', 'HEADS_MARKER': '${{ steps.probe.outputs.heads_marker }}',
                     'DEFAULT_BRANCH': '${{ github.event.repository.default_branch }}'},
             'run': 'python3 tools/upstream_pr_guard.py --heads-marker "$HEADS_MARKER" --base "$DEFAULT_BRANCH" --github-output "$GITHUB_OUTPUT"'},
            {'name': '准备独立官方更新', 'id': 'update',
             'if': "steps.probe.outputs.has_updates == 'true' && steps.guard.outputs.already_pending != 'true'",
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
python3 - <<'PY'
import json,os,pathlib
path = pathlib.Path(os.environ['RUNNER_TEMP']) / 'official-update.md'
official_ok = os.environ['OFFICIAL_RESULT'] == 'success'
catalog_ok = os.environ['CATALOG_RESULT'] == 'success'
status = ('SukiSU/SUSFS：' + ('检查完成' if official_ok else '检查失败，未确认有无更新') +
          '；Android GKI：' + ('检查完成' if catalog_ok else '检查失败，未确认有无更新'))
if not (official_ok and catalog_ok):
    status += '。本次仅提交成功检查部分的候选；失败部分须重试，不能视为全部官方源已检查。'
body = ('<!-- gki-upstream-sync:v1 -->\\n' + os.environ['HEADS_MARKER'] + '\\n\\n'
        '# 官方源更新\\n\\n本 PR 直接跟踪 SukiSU、SUSFS 和 Android GKI 官方源，不依赖其他构建仓库。\\n\\n'
        + status + '\\n\\n'
        '新增 GKI 版本已核对官方 Makefile 并固定源码提交。SukiSU/SUSFS 候选仅记录新版本；合并候选记录不会切换已验证的构建源码，仍须完成接口适配和实际编译验证。\\n\\n'
        '## 官方候选快照\\n```json\\n' + json.dumps(json.loads(os.environ['OFFICIAL_CANDIDATES']) if official_ok else None,ensure_ascii=False,indent=2) + '\\n```\\n\\n'
        '## 新增内核版本\\n```json\\n' + json.dumps(json.loads(os.environ['CATALOG_CANDIDATES']) if catalog_ok else [],ensure_ascii=False,indent=2) + '\\n```\\n')
path.write_text(body,encoding='utf-8')
with open(os.environ['GITHUB_OUTPUT'],'a') as output:
    output.write(f'has_updates=true\\nbody_path={path}\\ntitle=Update official sources and GKI kernel catalog\\n')
PY
'''},
            {'name': '检查更新令牌', 'if': "steps.update.outputs.has_updates == 'true'",
             'env': {'SYNC_TOKEN': '${{ secrets.UPSTREAM_SYNC_TOKEN }}'},
             'run': 'test -n "$SYNC_TOKEN" || { echo "::error::请配置 UPSTREAM_SYNC_TOKEN 以创建官方更新 PR"; exit 1; }'},
            {'name': '创建或更新官方源 PR', 'id': 'pr', 'if': "steps.update.outputs.has_updates == 'true'",
             'uses': 'peter-evans/create-pull-request@v8',
             'with': {'token': '${{ secrets.UPSTREAM_SYNC_TOKEN }}', 'branch': 'automation/upstream-sync',
                      'base': '${{ github.event.repository.default_branch }}',
                      'title': '${{ steps.update.outputs.title }}', 'body-path': '${{ steps.update.outputs.body_path }}',
                      'commit-message': 'Track official source updates and new GKI kernels',
                      'assignees': '${{ github.repository_owner }}',
                      'add-paths': 'data/kernels.json\ndata/upstream-candidates.json\n'}},
            {'name': '提醒所有者审核官方更新',
             'if': "steps.pr.outputs.pull-request-operation == 'created' || steps.pr.outputs.pull-request-operation == 'updated'",
             'env': {'GH_TOKEN': '${{ github.token }}', 'PR_NUMBER': '${{ steps.pr.outputs.pull-request-number }}',
                     'REPOSITORY_OWNER': '${{ github.repository_owner }}'},
             'run': '''gh api --method POST "repos/$GITHUB_REPOSITORY/issues/$PR_NUMBER/comments" \\
  --raw-field body="@$REPOSITORY_OWNER 有新的官方源或 GKI 版本可审核，请查看本 PR 的适配说明。此次检查没有编译内核。"
'''},
            {'name': '记录检查结果',
             'if': 'always() && !cancelled()',
             'env': {'PR_URL': '${{ steps.pr.outputs.pull-request-url }}',
                     'HAS_UPDATES': '${{ steps.probe.outputs.has_updates }}',
                     'EXISTING_PR': '${{ steps.guard.outputs.pull_request_url }}',
                     'OFFICIAL_RESULT': '${{ steps.official.outcome }}',
                     'CATALOG_RESULT': '${{ steps.catalog.outcome }}'},
             'run': '''if [ -n "$PR_URL" ]; then
  printf '已准备官方更新 PR：%s\\n' "$PR_URL" >> "$GITHUB_STEP_SUMMARY"
elif [ -n "$EXISTING_PR" ]; then
  printf '相同官方版本已有待审核 PR：%s\\n' "$EXISTING_PR" >> "$GITHUB_STEP_SUMMARY"
elif [ "$OFFICIAL_RESULT" = success ] && [ "$CATALOG_RESULT" = success ] && [ "$HAS_UPDATES" = false ]; then
  echo '未发现新的官方源或 GKI 版本。' >> "$GITHUB_STEP_SUMMARY"
elif [ "$HAS_UPDATES" = true ]; then
  echo '已发现官方更新候选，但尚无可用 PR；请检查前面的分支归属、令牌或 PR 创建步骤。' >> "$GITHUB_STEP_SUMMARY"
fi
if [ "$OFFICIAL_RESULT" != success ] || [ "$CATALOG_RESULT" != success ]; then
  printf '\\n部分官方检查未完成：SukiSU/SUSFS=%s，Android GKI=%s。已成功检测的候选可审核，失败部分须重试；不能确认全部官方源是否更新。\\n' "$OFFICIAL_RESULT" "$CATALOG_RESULT" >> "$GITHUB_STEP_SUMMARY"
fi
'''},
            {'name': '提示未完成的官方检查',
             'if': "always() && !cancelled() && (steps.official.outcome == 'failure' || steps.catalog.outcome == 'failure')",
             'run': '''echo '::error::部分官方源检查失败；请查看失败步骤的 HTTP 或数据错误并重试。成功部分的更新候选可能已创建 PR。'
exit 1
'''},
        ],
    }


def release_job():
    condition = ("always() && !cancelled() && github.event_name == 'workflow_dispatch' && "
                 "needs.prepare-matrix.result == 'success' && "
                 "((inputs.operation == '编译内核' && needs.build-kernel.result == 'success' && "
                 "(inputs.kernelsu_variant != 'SukiSU' || needs.build-sukisu-manager.result == 'success')) || "
                 "(inputs.operation == '仅编译 SukiSU 安装包' && needs.build-sukisu-manager.result == 'success'))")
    return {
        'name': '发布 APK 与详细内核配置', 'if': condition,
        'needs': ['prepare-matrix', 'build-sukisu-manager', 'build-kernel'],
        'runs-on': 'ubuntu-24.04', 'timeout-minutes': 15,
        'permissions': {'contents': 'write'},
        'steps': [
            {'name': '检出发布工具', 'uses': 'actions/checkout@v6', 'with': {'persist-credentials': False}},
            {'name': '收集本次构建产物', 'uses': 'actions/download-artifact@v6',
             'with': {'pattern': 'Release-*', 'path': 'release-input', 'merge-multiple': False}},
            {'name': '整理直接下载文件与实际配置',
             'run': 'python3 tools/prepare_release.py --input release-input --output release-publish'},
            {'name': '发布独立安装包和刷机包', 'id': 'release',
             'env': {'GH_TOKEN': '${{ secrets.UPSTREAM_SYNC_TOKEN || github.token }}', 'RELEASE_TAG': 'gki-${{ github.run_id }}-${{ github.run_attempt }}'},
             'run': '''set -euo pipefail
title="$(cat release-publish/title.txt)"
gh release create "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" --target "$GITHUB_SHA" \\
  --draft --title "$title" --notes-file release-publish/release.md
assets=(release-publish/assets/*)
gh release upload "$RELEASE_TAG" "${assets[@]}" --repo "$GITHUB_REPOSITORY"
gh release edit "$RELEASE_TAG" --draft=false --latest --repo "$GITHUB_REPOSITORY"
url="https://github.com/$GITHUB_REPOSITORY/releases/tag/$RELEASE_TAG"
printf 'release_url=%s\\n' "$url" >> "$GITHUB_OUTPUT"
printf '直接下载 APK、AK3 及详细构建配置：[打开 Release](%s)\\n' "$url" >> "$GITHUB_STEP_SUMMARY"
'''},
        ],
    }

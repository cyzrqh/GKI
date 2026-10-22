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
            {'name': '检查 SukiSU 与 SUSFS 官方更新', 'id': 'official',
             'env': {'GH_TOKEN': '${{ github.token }}'},
             'run': 'python3 tools/check_official_updates.py --check-only'},
            {'name': '检查 Android 官方 GKI 版本', 'id': 'catalog',
             'run': 'python3 tools/update_kernel_catalog.py --check --github-output "$GITHUB_OUTPUT"'},
            {'name': '汇总官方更新', 'id': 'probe',
             'env': {'OFFICIAL_CHANGED': '${{ steps.official.outputs.has_updates }}',
                     'CATALOG_CHANGED': '${{ steps.catalog.outputs.has_updates }}',
                     'OFFICIAL_CANDIDATES': '${{ steps.official.outputs.candidates_json }}',
                     'CATALOG_CANDIDATES': '${{ steps.catalog.outputs.candidates_json }}'},
             'run': '''python3 - <<'PY'
import hashlib,json,os
changed = os.environ['OFFICIAL_CHANGED'] == 'true' or os.environ['CATALOG_CHANGED'] == 'true'
snapshot = [json.loads(os.environ['OFFICIAL_CANDIDATES']), json.loads(os.environ['CATALOG_CANDIDATES'])]
digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
    output.write(f"has_updates={str(changed).lower()}\\nheads_marker=<!-- gki-official-heads:sha256={digest} -->\\n")
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
                     'HEADS_MARKER': '${{ steps.probe.outputs.heads_marker }}'},
             'run': '''set -euo pipefail
python3 tools/check_official_updates.py --prepare --candidates-json "$OFFICIAL_CANDIDATES" --report-dir "$RUNNER_TEMP"
python3 tools/update_kernel_catalog.py --apply-candidates-json "$CATALOG_CANDIDATES"
python3 - <<'PY'
import json,os,pathlib
path = pathlib.Path(os.environ['RUNNER_TEMP']) / 'official-update.md'
body = ('<!-- gki-upstream-sync:v1 -->\\n' + os.environ['HEADS_MARKER'] + '\\n\\n'
        '# 官方源更新\\n\\n本 PR 直接跟踪 SukiSU、SUSFS 和 Android GKI 官方源，不依赖其他构建仓库。\\n\\n'
        '新增 GKI 版本已核对官方 Makefile 并固定源码提交。SukiSU/SUSFS 候选仅记录新版本；合并候选记录不会切换已验证的构建源码，仍须完成接口适配和实际编译验证。\\n\\n'
        '## 官方候选快照\\n```json\\n' + json.dumps(json.loads(os.environ['OFFICIAL_CANDIDATES']),ensure_ascii=False,indent=2) + '\\n```\\n\\n'
        '## 新增内核版本\\n```json\\n' + json.dumps(json.loads(os.environ['CATALOG_CANDIDATES']),ensure_ascii=False,indent=2) + '\\n```\\n')
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
             'env': {'PR_URL': '${{ steps.pr.outputs.pull-request-url }}',
                     'HAS_UPDATES': '${{ steps.probe.outputs.has_updates }}',
                     'EXISTING_PR': '${{ steps.guard.outputs.pull_request_url }}'},
             'run': '''if [ -n "$PR_URL" ]; then
  printf '已准备官方更新 PR：%s\\n' "$PR_URL" >> "$GITHUB_STEP_SUMMARY"
elif [ -n "$EXISTING_PR" ]; then
  printf '相同官方版本已有待审核 PR：%s\\n' "$EXISTING_PR" >> "$GITHUB_STEP_SUMMARY"
else
  echo '未发现新的官方源或 GKI 版本。' >> "$GITHUB_STEP_SUMMARY"
fi
'''},
        ],
    }


def release_job():
    condition = ("always() && !cancelled() && github.event_name == 'workflow_dispatch' && "
                 "needs.prepare-matrix.result == 'success' && "
                 "((inputs.operation == '编译内核' && needs.build-kernel.result == 'success') || "
                 "(inputs.operation == '仅编译 SukiSU 安装包' && needs.build-sukisu-manager.result == 'success'))")
    return {
        'name': '发布 APK 与详细内核配置', 'if': condition,
        'needs': ['prepare-matrix', 'build-sukisu-manager', 'build-kernel'],
        'runs-on': 'ubuntu-24.04', 'timeout-minutes': 15,
        'permissions': {'contents': 'write'},
        'steps': [
            {'name': '检出发布工具', 'uses': 'actions/checkout@v6', 'with': {'persist-credentials': False}},
            {'name': '收集本次构建产物', 'uses': 'actions/download-artifact@v6',
             'with': {'pattern': 'Release-*', 'path': 'release-input'}},
            {'name': '整理直接下载文件与实际配置',
             'run': 'python3 tools/prepare_release.py --input release-input --output release-publish'},
            {'name': '发布独立安装包和刷机包', 'id': 'release',
             'env': {'GH_TOKEN': '${{ github.token }}', 'RELEASE_TAG': 'gki-${{ github.run_id }}-${{ github.run_attempt }}'},
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

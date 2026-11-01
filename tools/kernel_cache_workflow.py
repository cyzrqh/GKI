"""Persistent object storage shared by the GKI and Oplus build workflows."""


def persistent_step(kind, operation, directory, key, condition, *, name=None, share_variants=False):
    return {
        'name': name or ('恢复' if operation == 'restore' else '保存') + '独立仓库编译缓存（' + kind + '）',
        'id': 'persistent-' + kind + '-' + operation,
        'if': condition,
        'continue-on-error': True, 'timeout-minutes': 8,
        'env': {'CACHE_REPOSITORY': '${{ github.repository_owner }}/gki-cache',
                'CACHE_TOKEN': '${{ secrets.UPSTREAM_SYNC_TOKEN }}',
                'OBJECT_CACHE_KEY': key, 'OBJECT_CACHE_DIRECTORY': directory},
        'run': 'python3 "$GITHUB_WORKSPACE/tools/persistent_kernel_cache.py" ' + operation
               + ' --kind ' + kind + ' --directory "$OBJECT_CACHE_DIRECTORY" --key "$OBJECT_CACHE_KEY"'
               + (' --share-variants' if share_variants and operation == 'restore' and kind == 'ccache' else ''),
    }


def wire_gki_cache(job):
    steps = job['steps']
    selection = next(step for step in steps if step.get('id') == 'ccache-config')
    selection['name'] = '选择实际编译系统的对象缓存'
    selection['run'] = '''set -euo pipefail
if [ -f "$KERNEL_ROOT/build/build.sh" ]; then
  echo 'enabled=true' >> "$GITHUB_OUTPUT"
else
  python3 "$GITHUB_WORKSPACE/tools/kleaf_compile_cache.py" \\
    --source "$KERNEL_ROOT" --program "$RUNNER_TEMP/gki-ccache-ecs/ccache" \\
    --output "$RUNNER_TEMP/gki-ccache-ecs/gki-make" --directory "$CCACHE_DIR"
fi
'''
    # ccache objects are already compressed: retain headroom below the
    # individual release-asset limit instead of failing after a warm build.
    job['env']['CCACHE_MAXSIZE'] = '1536M'
    statistics = next(step for step in steps if step.get('name') == '显示本次 ccache 命中统计')
    statistics['run'] = statistics['run'].replace(
        'report="$RUNNER_TEMP/gki-ccache-statistics.txt"',
        'ccache --max-size "$CCACHE_MAXSIZE"\nccache --cleanup\nreport="$RUNNER_TEMP/gki-ccache-statistics.txt"')
    statistics['run'] = statistics['run'].replace(
        '以下命中、未命中及不可缓存数量仅统计本次 build.sh 编译；Bazel 使用单独的磁盘缓存。',
        '以下命中、未命中及不可缓存数量统计本次实际内核编译；build.sh 和已接入的 Kleaf 均使用逐文件缓存，Bazel 另有完整任务磁盘缓存。')
    measurement = next(step for step in steps if step.get('id') == 'ccache-measurement')
    measurement['run'] = '''set -euo pipefail
echo "Available build CPUs: $(nproc)"
free -h
ccache --zero-stats
ccache --show-config
echo 'enabled=true' >> "$GITHUB_OUTPUT"
'''
    compile_step = next(step for step in steps if step.get('id') == 'compile-kernel')
    if compile_step.get('uses') == 'nick-fields/retry@v4':
        settings = compile_step.pop('with')
        compile_step.pop('uses')
        compile_step['run'] = settings['command']
        # Repeating preparation after a timeout can consume/reset the saved
        # defconfig fragment and conceals another complete kernel build.
        compile_step['timeout-minutes'] = 100
    bazel = 'tools/bazel build --disk_cache="$BAZEL_DISK_CACHE"'
    if compile_step['run'].count(bazel) != 1:
        raise ValueError('Expected the official GKI Bazel build command')
    compile_step['run'] = compile_step['run'].replace(bazel,
        'if [ -n "${CCACHE_PROGRAM:-}" ]; then echo \'CCACHE_COMPILE_STARTED=true\' >> "$GITHUB_ENV"; fi\n    ' + bazel)
    native = 'build/build.sh CC="${CCACHE_PROGRAM:-/usr/bin/ccache} clang"'
    if compile_step['run'].count(native) != 1:
        raise ValueError('Expected the official build.sh compiler command')
    compile_step['run'] = compile_step['run'].replace(native,
        'python3 "$GITHUB_WORKSPACE/tools/run_kernel_build.py" --label "build.sh 内核编译" -- ' + native)
    compile_step['run'] = compile_step['run'].replace(bazel,
        'python3 "$GITHUB_WORKSPACE/tools/run_kernel_build.py" --label "Bazel 内核编译" -- ' + bazel)
    for kind, restore_name, save_name, directory, prefix in (
            ('ccache', '恢复 ccache 缓存', '保存本次 ccache 编译结果', '${{ env.CCACHE_DIR }}', 'gki-ccache-ecs-v1-'),
            ('bazel', '恢复 Bazel 磁盘缓存', '保存 Bazel 磁盘缓存', '${{ env.BAZEL_DISK_CACHE }}', 'gki-bazel-v1-')):
        restore = next(step for step in steps if step.get('name') == restore_name)
        save = next(step for step in steps if step.get('name') == save_name)
        key = restore['with']['key'].removesuffix('${{ github.run_id }}-${{ github.run_attempt }}')
        if not key.startswith(prefix):
            raise ValueError('Unexpected persistent cache namespace')
        key += '${{ inputs.kernelsu_variant }}'
        restore_condition, save_condition = restore['if'], save['if']
        restore['if'] += " && steps.persistent-" + kind + "-restore.outputs.restored != 'true'"
        save['if'] += " && steps.persistent-" + kind + "-save.outputs.saved != 'true'"
        # Restoring unrelated complete kernel versions cannot yield a guarded
        # hit and copies their objects into every subsequent snapshot.
        restore['with']['restore-keys'] = '\n'.join(restore['with']['restore-keys'].splitlines()[:2]) + '\n'
        steps.insert(steps.index(restore), persistent_step(kind, 'restore', directory, key, restore_condition,
                                                         share_variants=kind == 'ccache'))
        steps.insert(steps.index(save), persistent_step(kind, 'save', directory, key, save_condition))
    # Stay below GitHub's 2GiB per release-asset bound, including tar metadata.
    trim = next(step for step in steps if step.get('id') == 'bazel-cache-trim')
    trim['env']['BAZEL_CACHE_MAX_BYTES'] = str(1536 * 1024 * 1024)
    key = next(step for step in steps if step.get('id') == 'persistent-ccache-restore')['env']['OBJECT_CACHE_KEY']
    wire_thinlto(job, key.replace('gki-ccache-ecs-v1-', 'gki-thinlto-v1-'), oplus=False)


def wire_thinlto(job, key, *, oplus):
    """Keep Make's LTO results separate from ccache and all published assets."""
    steps = job['steps']
    job['env']['GKI_THINLTO_CACHE_DIR'] = '${{ github.workspace }}/.gki-thinlto-cache'
    # Inline assembly may .incbin generated payloads that LLVM cannot hash.
    # Never share link results across manager revisions or Kbuild/config edits.
    key += ("-${{ needs.prepare-matrix.outputs.ksu_ref }}-"
            "${{ hashFiles('**/common/arch/arm64/configs/gki_defconfig', '**/KernelSU/kernel/Kbuild', "
            "'tools/thinlto_cache.py') }}")
    condition = 'success()' if oplus else ("success() && steps.ccache-config.outputs.enabled == 'true'"
                                         " && steps.bazel-cache-config.outputs.enabled != 'true'")
    source = '$GITHUB_WORKSPACE/kernel_workspace/common' if oplus else '$KERNEL_ROOT/common'
    compile_id = 'oplus-build' if oplus else 'compile-kernel'
    compile_step = next(step for step in steps if step.get('id') == compile_id)
    setup = {'name': '配置独立 ThinLTO 链接缓存', 'id': 'thinlto-config', 'if': condition,
             'continue-on-error': True,
             'run': 'python3 "$GITHUB_WORKSPACE/tools/thinlto_cache.py" configure --source "' + source
                    + '" --directory "$GKI_THINLTO_CACHE_DIR"'}
    ready = "success() && steps.thinlto-config.outputs.enabled == 'true'"
    restore = persistent_step('thinlto', 'restore', '${{ env.GKI_THINLTO_CACHE_DIR }}', key, ready)
    restore['timeout-minutes'] = 3
    report = {'name': '显示本次恢复的 ThinLTO 链接对象', 'if': ready, 'continue-on-error': True,
              'run': 'python3 "$GITHUB_WORKSPACE/tools/thinlto_cache.py" report --directory "$GKI_THINLTO_CACHE_DIR"'}
    index = steps.index(compile_step)
    steps[index:index] = [setup, restore, report]
    successful = "always() && !cancelled() && steps.thinlto-config.outputs.enabled == 'true' && steps." + compile_id + ".outcome == 'success'"
    trim = {'name': '限制并统计 ThinLTO 链接缓存', 'id': 'thinlto-trim', 'if': successful,
            'continue-on-error': True,
            'run': 'python3 "$GITHUB_WORKSPACE/tools/thinlto_cache.py" trim --directory "$GKI_THINLTO_CACHE_DIR"'}
    save = persistent_step('thinlto', 'save', '${{ env.GKI_THINLTO_CACHE_DIR }}', key, successful)
    save['timeout-minutes'] = 3
    index = next(i for i, step in enumerate(steps) if step.get('id') == 'persistent-ccache-save')
    steps[index:index] = [trim, save]

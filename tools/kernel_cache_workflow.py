"""Persistent object storage shared by the GKI and Oplus build workflows."""


def persistent_step(kind, operation, directory, key, condition, *, name=None):
    return {
        'name': name or ('恢复' if operation == 'restore' else '保存') + '独立仓库编译缓存（' + kind + '）',
        'id': 'persistent-' + kind + '-' + operation,
        'if': condition,
        'continue-on-error': True, 'timeout-minutes': 8,
        'env': {'CACHE_REPOSITORY': '${{ github.repository_owner }}/gki-cache',
                'CACHE_TOKEN': '${{ secrets.UPSTREAM_SYNC_TOKEN }}',
                'OBJECT_CACHE_KEY': key, 'OBJECT_CACHE_DIRECTORY': directory},
        'run': 'python3 "$GITHUB_WORKSPACE/tools/persistent_kernel_cache.py" ' + operation
               + ' --kind ' + kind + ' --directory "$OBJECT_CACHE_DIRECTORY" --key "$OBJECT_CACHE_KEY"',
    }


def wire_gki_cache(job):
    steps = job['steps']
    # ccache objects are already compressed: retain headroom below the
    # individual release-asset limit instead of failing after a warm build.
    job['env']['CCACHE_MAXSIZE'] = '1536M'
    statistics = next(step for step in steps if step.get('name') == '显示本次 ccache 命中统计')
    statistics['run'] = statistics['run'].replace(
        'report="$RUNNER_TEMP/gki-ccache-statistics.txt"',
        'ccache --max-size "$CCACHE_MAXSIZE"\nccache --cleanup\nreport="$RUNNER_TEMP/gki-ccache-statistics.txt"')
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
        steps.insert(steps.index(restore), persistent_step(kind, 'restore', directory, key, restore_condition))
        steps.insert(steps.index(save), persistent_step(kind, 'save', directory, key, save_condition))
    # Stay below GitHub's 2GiB per release-asset bound, including tar metadata.
    trim = next(step for step in steps if step.get('id') == 'bazel-cache-trim')
    trim['env']['BAZEL_CACHE_MAX_BYTES'] = str(1536 * 1024 * 1024)

#!/usr/bin/env python3
"""Prepare independent source updates; roll back only a failed source's files."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from update_oplus_sources import allowed_paths, PROFILE_PATH

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ('official', 'catalog', 'susfs', 'oplus')
STATIC_FILES = {
    'official': ('data/upstream-candidates.json',),
    'catalog': ('data/kernels.json',),
    'susfs': ('build-compatibility.json', 'config/manager-susfs-pins.json', 'data/susfs-upstream-candidates.json'),
}


class PreparationError(ValueError):
    pass


def checked_path(root: Path, relative: str) -> Path:
    path = root / relative
    if path.resolve().parent != path.parent.resolve() or not path.resolve().is_relative_to(root):
        raise PreparationError('Source update path escaped the repository')
    for item in (path, *path.parents):
        if item == root: break
        if item.is_symlink() or hasattr(item, 'is_junction') and item.is_junction():
            raise PreparationError('Source update paths cannot contain links')
    if path.exists() and not path.is_file():
        raise PreparationError('Source update destination must be a regular file')
    return path


def read_files(root: Path, paths):
    return {name: path.read_bytes() if path.exists() else None
            for name in paths for path in (checked_path(root, name),)}


def restore_files(root: Path, originals):
    for relative, content in originals.items():
        path = checked_path(root, relative)
        if content is None:
            path.unlink(missing_ok=True)
        elif not path.exists() or path.read_bytes() != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.source-restore-', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            try:
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    if read_files(root, originals) != originals:
        raise PreparationError('Failed source files could not be restored exactly')


def command(source, root, candidates, report_dir):
    arguments = [sys.executable, str(ROOT / 'tools' / {
        'official': 'check_official_updates.py', 'catalog': 'update_kernel_catalog.py',
        'susfs': 'update_susfs_sources.py', 'oplus': 'update_oplus_sources.py'}[source]), '--root', str(root)]
    if source == 'official':
        arguments += ['--prepare', '--candidates-json', candidates, '--report-dir', str(report_dir)]
    else:
        arguments += ['--apply-candidates-json', candidates]
    return arguments


def prepare(root: Path, environment: dict, report_dir: Path, *, runner=None) -> dict:
    if root.is_symlink() or not root.is_dir():
        raise PreparationError('Preparation requires a real repository directory')
    root = root.resolve(strict=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    files = dict(STATIC_FILES)
    profiles = json.loads(checked_path(root, PROFILE_PATH).read_text(encoding='utf-8'))
    files['oplus'] = tuple(sorted(allowed_paths(profiles)))
    if set(files['oplus']) & {name for source in STATIC_FILES.values() for name in source}:
        raise PreparationError('Source update file scopes overlap')
    runner = runner or subprocess.run
    child_environment = {name: value for name, value in environment.items() if name != 'GITHUB_OUTPUT'}
    result = {'schema_version': 1, 'sources': {}, 'has_updates': False, 'partial_failure': False}
    for source in SOURCES:
        check = environment.get(source.upper() + '_RESULT', 'skipped')
        flag = environment.get(source.upper() + '_CHANGED', 'false')
        if check not in ('success', 'failure', 'skipped', 'cancelled', 'timed_out') or flag not in ('true', 'false', ''):
            raise PreparationError('Invalid source check state or update flag')
    for source in SOURCES:
        prefix = source.upper()
        check = environment.get(prefix + '_RESULT', 'skipped')
        flag = environment.get(prefix + '_CHANGED', 'false')
        state = {'check_result': check, 'changed': flag == 'true', 'prepared': False,
                 'result': 'success' if check == 'success' else 'skipped' if check == 'skipped' else 'failure',
                 'changed_files': [], 'error': ''}
        result['sources'][source] = state
        if check != 'success':
            state['error'] = '来源检查未完成，未准备该来源' if check != 'skipped' else '本次未检查该来源'
            result['partial_failure'] = True
            continue
        if flag != 'true': continue
        original = read_files(root, files[source])
        try:
            candidates = environment.get(prefix + '_CANDIDATES', '')
            if not candidates:
                raise PreparationError('Missing frozen candidates for a changed source')
            completed = runner(command(source, root, candidates, report_dir), cwd=root,
                               env=child_environment, timeout=600, check=False)
            if completed.returncode:
                raise PreparationError('来源准备命令失败（退出码 ' + str(completed.returncode) + '）')
            current = read_files(root, files[source])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            try:
                restore_files(root, original)
            except (OSError, ValueError) as rollback:
                raise PreparationError('来源 ' + source + ' 准备失败且无法恢复原文件；停止创建更新 PR') from rollback
            state.update(result='failure', error=('来源准备超时' if isinstance(error, subprocess.TimeoutExpired)
                         else str(error) if isinstance(error, PreparationError) else '来源准备未完成；原文件已恢复'))
            result['partial_failure'] = True
            print('::warning::' + source + '：' + state['error'] + '；已恢复本来源文件，继续其它来源。', flush=True)
            continue
        state['prepared'] = True
        state['changed_files'] = [path for path in original if original[path] != current[path]]
        result['has_updates'] |= bool(state['changed_files'])
    return result


def outputs(result: dict):
    return {**{source + '_result': result['sources'][source]['result'] for source in SOURCES},
            'has_updates': str(result['has_updates']).lower(),
            'partial_failure': str(result['partial_failure']).lower()}


def render_report(result: dict) -> str:
    labels = {'official': '管理器 / SUSFS 官方记录', 'catalog': 'Android GKI 目录',
              'susfs': 'SUSFS 构建来源', 'oplus': '欧加真来源'}
    lines = ['### 分来源准备结果', '', '| 来源 | 检查 | 准备 | 实际变动文件数 | 说明 |',
             '| --- | --- | --- | --- | --- |']
    for source in SOURCES:
        state = result['sources'][source]
        checked = '完成' if state['check_result'] == 'success' else '未检查' if state['check_result'] == 'skipped' else '失败'
        count = len(state['changed_files'])
        if state['check_result'] != 'success':
            status, note = '未运行', '检查未完成，保留该来源原记录。'
        elif state['result'] == 'failure':
            status, note = '失败', '已恢复该来源原文件；其它来源继续处理。'
        elif not state['changed']:
            status, note = '无需变更', '没有需要准备的新变更。'
        else:
            status = '完成'
            note = '已保留本次验证通过的变更，等待后续同步。' if count else '当前记录已是最新。'
        lines.append('| ' + labels[source] + ' | ' + checked + ' | ' + status + ' | ' + str(count) + ' | ' + note + ' |')
    if result['partial_failure']:
        lines += ['', '**部分来源未完成。本次仅保留准备成功的变更；失败来源没有同步，仍需重试。**']
    else:
        lines += ['', '来源准备完成。是否合并，由后续验证和手动同步步骤决定。']
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.root, dict(os.environ), args.report_dir)
        report = args.report_dir / 'source-preparation.json'
        markdown = args.report_dir / 'source-preparation.md'
        if report.is_symlink() or markdown.is_symlink(): raise PreparationError('Preparation report must be a regular file')
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        markdown.write_text(render_report(result), encoding='utf-8', newline='\n')
        if args.github_output:
            if args.github_output.is_symlink(): raise PreparationError('GitHub output must be a regular file')
            with args.github_output.open('a', encoding='utf-8', newline='\n') as stream:
                stream.write(''.join(key + '=' + value + '\n' for key, value in outputs(result).items()))
        print(json.dumps(outputs(result), ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError) as error:
        print('::error::来源更新准备已停止：' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

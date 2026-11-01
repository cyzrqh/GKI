"""Default only manager update checks off; keep module updates independent."""
from __future__ import annotations

import argparse
from pathlib import Path
import re

from manager_variants import MANAGERS, manager_profile


def plan_defaults(source: Path, variant: str) -> dict[Path, str]:
    manager_profile(variant)
    java = source / 'manager/app/src/main/java'
    if java.is_symlink() or not java.is_dir():
        raise ValueError('Missing native manager Kotlin source')
    changes = {}
    observed = 0
    for path in java.rglob('*.kt'):
        if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('Unsafe native manager source input')
        original = path.read_text(encoding='utf-8')
        text = original
        # BakaSU migrated module preferences from the manager preference.
        # Decouple that legacy fallback BEFORE changing manager defaults, so
        # turning manager checks off never turns module checks off as well.
        text = re.sub(
            r'(getBooleanPreference\(\s*PREF_CHECK_MODULE_UPDATE\s*,\s*)'
            r'getBooleanPreference\(\s*PREF_CHECK_UPDATE\s*,\s*(?:true|false)\s*\)',
            r'\1true', text)
        text = re.sub(
            r'(getBoolean\(\s*"(?:check_module_update|module_check_update)"\s*,\s*)'
            r'(?:settings|prefs)\.getBoolean\(\s*"check_update"\s*,\s*(?:true|false)\s*\)',
            r'\1true', text)
        literal = re.compile(r'((?:getBoolean|getBooleanPreference)\(\s*'
                             r'"(?:check_update|check_beta_update)"\s*,\s*)(true|false)(\s*\))')
        symbolic = re.compile(r'(getBooleanPreference\(\s*PREF_CHECK_(?:BETA_)?UPDATE\s*,\s*)'
                              r'(true|false)(\s*\))')
        # Module code is only changed above to remove a manager dependency;
        # its own defaults, requests, update JSON and notifications stay intact.
        if 'ModuleViewModel' not in path.name:
            observed += len(literal.findall(text)) + len(symbolic.findall(text))
            text = literal.sub(r'\1false\3', text)
            text = symbolic.sub(r'\1false\3', text)
            text = re.sub(r'(\b(?:checkUpdate|checkUpdateEnabled|checkManagerUpdate|checkBetaUpdate)'
                          r'\s*:\s*Boolean\s*=\s*)true\b', r'\1false', text)
        if text != original:
            changes[path] = text
    if not observed:
        raise ValueError('Review changed upstream manager update settings; no known preference was found')
    resources = source / 'manager/app/src/main/res'
    for path in resources.glob('values-zh*/strings.xml'):
        original = path.read_text(encoding='utf-8')
        text = original.replace('加载仅本次开机有效；嵌入会保存模块与参数，并在重启后自动加载。',
                                '嵌入会保存模块与参数，并在重启后自动加载。')
        if text != original:
            changes[path] = text
    return changes


def apply_defaults(source: Path, variant: str) -> list[str]:
    changes = plan_defaults(source, variant)
    for path, text in changes.items():
        path.write_text(text, encoding='utf-8', newline='\n')
    return [str(path.relative_to(source)).replace('\\', '/') for path in changes]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--variant', required=True, choices=MANAGERS)
    args = parser.parse_args()
    for name in apply_defaults(args.source.resolve(strict=True), args.variant):
        print('Manager defaults: ' + name)

"""Keep native package IDs while building one unsigned ARM64 APK."""
import argparse
from pathlib import Path
import re
from manager_variants import ADDITIONAL_MANAGERS, manager_profile


def prepare(source: Path, variant: str):
    if variant not in ADDITIONAL_MANAGERS:
        raise ValueError('Expected one additional source-built manager')
    app = source / 'manager/app/build.gradle.kts'
    text = app.read_text(encoding='utf-8')
    plugin = re.compile(r'^[ \t]*alias\(libs\.plugins\.lsplugin\.apksign\)[ \t]*\n', re.M)
    block = re.compile(r'^apksign\s*\{([^{}]*)\}\s*\n', re.M)
    if len(plugin.findall(text)) != 1 or len(block.findall(text)) != 1:
        raise ValueError('Review changed upstream release signing configuration')
    fields = dict(re.findall(r'(\w+)\s*=\s*"([A-Z_]+)"', block.search(text)[1]))
    if fields != {'storeFileProperty': 'KEYSTORE_FILE', 'storePasswordProperty': 'KEYSTORE_PASSWORD',
                  'keyAliasProperty': 'KEY_ALIAS', 'keyPasswordProperty': 'KEY_PASSWORD'}:
        raise ValueError('Unknown upstream signing configuration')
    text = plugin.sub('', block.sub('', text, count=1), count=1)
    text, count = re.subn(r'(?m)^([ \t]*)release\s*\{', r'\1release {\n\1    signingConfig = null', text)
    if count != 1:
        raise ValueError('Expected one native release build type')
    package = manager_profile(variant)['package']
    # Existing defaultConfig may be inherited from the root Gradle project.
    # finalizeDsl runs before AGP locks its model, including when defaults are
    # inherited from the root project. aapt2 verifies the resulting package.
    text += ('\nandroid { defaultConfig { applicationId = "' + package + '" } }\n'
             'androidComponents { finalizeDsl { extension ->\n'
             '    extension.defaultConfig.ndk.abiFilters.apply { clear(); add("arm64-v8a") }\n'
             '} }\n')
    app.write_text(text, encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--variant', required=True, choices=ADDITIONAL_MANAGERS)
    args = parser.parse_args()
    prepare(args.source, args.variant)

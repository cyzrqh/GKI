#!/usr/bin/env bash
# Build the complete official BakaSU manager and daemon; signing is separate.
set -euo pipefail
build_tools="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

source_dir=""
output_dir=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --source|--output)
      [ "$#" -ge 2 ] || { echo "Missing value for $1" >&2; exit 2; }
      if [ "$1" = --source ]; then source_dir="$2"; else output_dir="$2"; fi
      shift 2
      ;;
    -h|--help)
      echo 'Usage: build_bakasu_manager.sh --source ROOT --output DIR'
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$source_dir" ] && [ -n "$output_dir" ] || {
  echo 'Both --source and --output are required.' >&2
  exit 2
}
source_dir="$(cd "$source_dir" && pwd -P)"
mkdir -p "$output_dir"
output_dir="$(cd "$output_dir" && pwd -P)"
manager_dir="$source_dir/manager"
python3 "$build_tools/manager_ui_defaults.py" --source "$source_dir" --variant BakaSU
for required in userspace/ksud/Cargo.toml userspace/ksud/Cargo.lock userspace/ksuinit/Cargo.toml userspace/ksuinit/Cargo.lock .github/scripts/setup-rust-build.sh manager/gradlew manager/app/build.gradle.kts; do
  [ -f "$source_dir/$required" ] || { echo "Missing BakaSU build source: $required" >&2; exit 1; }
done
: "${ANDROID_HOME:?ANDROID_HOME must point to the prepared Android SDK}"
ANDROID_NDK_HOME="${ANDROID_NDK_HOME:-${ANDROID_NDK_ROOT:-}}"
: "${ANDROID_NDK_HOME:?ANDROID_NDK_HOME must point to the prepared Android NDK}"
export ANDROID_NDK_HOME
export CARGO_TARGET_DIR="$source_dir/target"
if [ "${KSU_PACKAGE_NAME:-com.resukisu.resukisu}" != com.resukisu.resukisu ]; then
  echo 'The BakaSU manager package must match the fixed kernel pairing.' >&2
  exit 1
fi
export KSU_PACKAGE_NAME=com.resukisu.resukisu
[ -d "$ANDROID_HOME" ] && [ -d "$ANDROID_NDK_HOME" ] || { echo 'Android SDK or NDK directory does not exist.' >&2; exit 1; }
native_ndk_dir="$ANDROID_HOME/ndk/29.0.14206865"
[ -d "$native_ndk_dir" ] || { echo 'BakaSU native Rust builds require the official NDK r29 toolchain.' >&2; exit 1; }

# The caller stages and validates the same-source, same-certificate modules.
# Refuse an incomplete asset directory before running either native compiler.
python3 - "$source_dir" "$build_tools" <<'PY'
from pathlib import Path
import struct
import sys
sys.path.insert(0, sys.argv[2])
from manager_lkm_jobs import KMIS
assets = Path(sys.argv[1]) / 'userspace/ksud/bin/aarch64'
expected = {kmi + '_kernelsu.ko' for kmi in KMIS}
actual = {path.name for path in assets.glob('*_kernelsu.ko')}
if actual != expected:
    raise SystemExit('The manager requires all eight verified native KMI modules; missing: '
                     + ', '.join(sorted(expected - actual)) + '; unexpected: '
                     + ', '.join(sorted(actual - expected)))
for name in sorted(expected):
    path = assets / name
    if path.is_symlink() or not path.is_file() or not 64 <= path.stat().st_size <= 64 * 1024 * 1024:
        raise SystemExit('Missing or invalid staged manager module: ' + name)
    with path.open('rb') as stream:
        header = stream.read(64)
    if (header[:7] != b'\x7fELF\x02\x01\x01'
            or struct.unpack_from('<HHI', header, 16) != (1, 183, 1)):
        raise SystemExit('The staged manager module is not an ARM64 ELF object: ' + name)
print('Manager native KMI inputs: ' + ', '.join(KMIS))
PY

cd "$source_dir"
echo "BakaSU manager source: $(git rev-parse HEAD)"
echo "BakaSU source version: $(git describe --tags --always --abbrev=0)"
java -version
rustc +nightly --version
cargo +nightly --version

# Verify the restored manager sources before the build-only ABI/signing edits.
(
cd "$manager_dir"
bash ./gradlew --no-daemon --stacktrace --build-cache spotlessCheck -PKSU_PACKAGE_NAME="$KSU_PACKAGE_NAME"
)

# Keep the complete official ABI/split configuration.
python3 - "$manager_dir/app/build.gradle.kts" <<'PY'
from pathlib import Path
import re
import sys
path = Path(sys.argv[1])
text = path.read_text(encoding='utf-8')
# AGP takes release ABI selection from the original splits block. Its second
# defaultConfig filter conflicts with splits; keep every split and universal
# output, and preserve the original filter for debug builds.
pattern = re.compile(r'\babiFilters\s*\+=\s*listOf\(([^)]*)\)')
matches = list(pattern.finditer(text))
splits = list(re.finditer(r'\binclude\(([^)]*)\)', text))
if (len(matches) != 1 or len(splits) != 1 or text.count('isUniversalApk = true') != 1
        or len(re.findall(r'(?m)^\s*splits\s*\{', text)) != 1
        or text.count('isEnable = isReleaseTask') != 1
        or set(re.findall(r'"([A-Za-z0-9_-]+)"', matches[0][1]))
           != set(re.findall(r'"([A-Za-z0-9_-]+)"', splits[0][1]))):
    raise SystemExit('Review the original BakaSU full-ABI split configuration.')
text = pattern.sub(lambda m: 'if (!isReleaseTask) { ' + m[0] + ' }', text, count=1)
# Without defaultConfig NDK filters AGP also puts dependency-only x86 JNI
# libraries in the universal APK. Apply the original ABI selection to native
# packaging as well, retaining all four source-declared architectures.
declared = set(re.findall(r'"([A-Za-z0-9_-]+)"', matches[0][1]))
undeclared = sorted({'armeabi', 'armeabi-v7a', 'arm64-v8a', 'x86', 'x86_64', 'riscv64', 'mips', 'mips64'} - declared)
if undeclared:
    excludes = ', '.join('"**/' + abi + '/**"' for abi in undeclared)
    text += '\nandroid { packaging { jniLibs { excludes += setOf(' + excludes + ') } } }\n'
# The upstream signing plugin falls back to the shared debug key when no
# keystore is present. Remove only its known declaration/configuration so the
# release remains unsigned until the separate private signing step.
plugin = re.compile(r'^[ \t]*alias\(libs\.plugins\.lsplugin\.apksign\)[ \t]*\r?\n', re.M)
config = re.compile(r'^apksign[ \t]*\{([^{}]*)\}[ \t]*\r?\n', re.M)
plugins, configs = list(plugin.finditer(text)), list(config.finditer(text))
expected = {'storeFileProperty': 'KEYSTORE_FILE', 'storePasswordProperty': 'KEYSTORE_PASSWORD',
            'keyAliasProperty': 'KEY_ALIAS', 'keyPasswordProperty': 'KEY_PASSWORD'}
if len(plugins) != 1 or len(configs) != 1:
    raise SystemExit('Expected the official APK signing plugin and configuration; review source drift.')
body = configs[0].group(1)
fields = re.findall(r'^\s*(\w+)\s*=\s*"([A-Z_]+)"\s*$', body, re.M)
if len(fields) != len(expected) or dict(fields) != expected:
    raise SystemExit('Unexpected APK signing configuration; refusing a partial rewrite.')
text = plugin.sub('', config.sub('', text, count=1), count=1)
if re.search(r'\b(?:apksign|signingConfig|signingConfigs)\b', text):
    raise SystemExit('Additional source signing configuration requires review.')
release = re.compile(r'^([ \t]*)release[ \t]*\{[ \t]*$', re.M)
if len(list(release.finditer(text))) != 1:
    raise SystemExit('Expected one release build type to keep unsigned.')
text = release.sub(lambda m: m.group(0) + '\n' + m.group(1) + '    signingConfig = null', text, count=1)
path.write_text(text, encoding='utf-8')
print('Official APK architecture configuration preserved')
print('Release signing: separate workflow step')
PY

bash "$build_tools/build_manager_native.sh" "$source_dir"

cd "$manager_dir"
echo 'Building the complete official-ABI release APK; signing is handled by the caller.'
bash ./gradlew --no-daemon --stacktrace --build-cache assembleRelease -PKSU_PACKAGE_NAME="$KSU_PACKAGE_NAME"

python3 - "$manager_dir/app/build/outputs/apk/release" "$output_dir" "$build_tools" <<'PY'
from pathlib import Path
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
sys.path.insert(0, sys.argv[3])
from manager_lkm_jobs import KMIS
from manager_full_native import official_abis, verify_apk
release, output = map(Path, sys.argv[1:3])
source = release.parents[5]
expected_abis = set(official_abis(source))
apks = []
for candidate in sorted(release.rglob('*.apk')):
    with zipfile.ZipFile(candidate) as archive:
        abis = {name.split('/')[1] for name in archive.namelist() if name.startswith('lib/') and name.count('/') >= 2}
    print('Native release output: ' + str(candidate.relative_to(release)) + '; ABIs=' + ','.join(sorted(abis)))
    if abis == expected_abis:
        apks.append(candidate)
if len(apks) != 1:
    raise SystemExit(f'Expected exactly one release APK, found {len(apks)}.')
apk = apks[0]
verify_apk(source, apk)
if 'unsigned' not in apk.stem.lower():
    raise SystemExit('Expected the unsigned release APK; signing material must remain outside the source build.')
if not apk.name.endswith('-unsigned.apk') or any(ord(char) < 32 or ord(char) > 126 for char in apk.name):
    raise SystemExit('Expected a printable official release-unsigned APK filename.')
if not apk.name.startswith(('BakaSU_', 'ReSukiSU_')) or not apk.name.endswith('-arm64-v8a-release-unsigned.apk'):
    raise SystemExit('Expected the original official BakaSU arm64 release APK filename.')
with zipfile.ZipFile(apk) as archive:
    required = {'AndroidManifest.xml', 'classes.dex', 'lib/arm64-v8a/libksud.so',
                'lib/arm64-v8a/libkernelsu.so', 'lib/arm64-v8a/libadbroot.so'}
    missing = required.difference(archive.namelist())
    if missing:
        raise SystemExit('Incomplete manager APK: ' + ', '.join(sorted(missing)))
    other_abis = {name.split('/')[1] for name in archive.namelist() if name.startswith('lib/') and len(name.split('/')) >= 3}
    if other_abis != expected_abis:
        raise SystemExit('The manager APK lost an official architecture.')
    if archive.getinfo('lib/arm64-v8a/libksud.so').file_size == 0:
        raise SystemExit('The packaged ksud is empty.')
    packaged_ksud = archive.read('lib/arm64-v8a/libksud.so')
    header = packaged_ksud[:20]
    if len(header) != 20 or header[:5] != b'\x7fELF\x02' or header[5] != 1 or struct.unpack_from('<H', header, 18)[0] != 183:
        raise SystemExit('The packaged ksud is not a little-endian ARM64 ELF binary.')
    missing_kmis = [kmi for kmi in KMIS if (kmi + '_kernelsu.ko').encode('ascii') not in packaged_ksud]
    if missing_kmis:
        raise SystemExit('The packaged ksud is missing embedded native KMI modules: ' + ', '.join(missing_kmis))
    if archive.testzip() is not None:
        raise SystemExit('The APK ZIP integrity check failed.')
sdk = Path(os.environ['ANDROID_HOME']) / 'build-tools/37.0.0'
aapt2 = next((path for path in (sdk / 'aapt2', sdk / 'aapt2.exe') if path.is_file()), None)
if aapt2 is None:
    raise SystemExit('Android build-tools 37.0.0 aapt2 was not prepared.')
report = subprocess.run([str(aapt2), 'dump', 'badging', str(apk)], capture_output=True,
                        text=True, encoding='utf-8', errors='replace', timeout=180)
if report.returncode:
    raise SystemExit('aapt2 could not read the built manager APK metadata.')
rows = re.findall(r'^package:[^\r\n]*', report.stdout, re.M)
if len(rows) != 1:
    raise SystemExit('Expected one package metadata row in the built manager APK.')
fields = {}
for name in ('name', 'versionName', 'versionCode'):
    values = re.findall(r'(?:^|\s)' + name + r"='([^'\r\n]*)'(?=\s|$)", rows[0])
    if len(values) != 1:
        raise SystemExit('Expected one built manager APK ' + name + '.')
    fields[name] = values[0]
if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', fields['name']):
    raise SystemExit('The built manager application ID is not a valid ASCII package.')
if fields['name'] != 'com.resukisu.resukisu':
    raise SystemExit('The BakaSU APK package does not match the fixed kernel pairing.')
version = fields['versionName']
if not version or len(version) > 128 or any(ord(char) < 32 or ord(char) > 126 for char in version):
    raise SystemExit('The built manager versionName must be nonempty printable ASCII.')
code = fields['versionCode']
if not re.fullmatch(r'[1-9][0-9]{0,9}', code) or int(code) > 2147483647:
    raise SystemExit('The built manager versionCode is invalid.')
metadata = {'source_apk_filename': apk.name,
            'apk_filename': apk.name.removesuffix('-unsigned.apk') + '.apk',
            'manager_package': fields['name'], 'manager_version_name': version,
            'manager_version_code': int(code)}
target = output / 'manager-unsigned.apk'
shutil.copyfile(apk, target)
(output / 'manager-build-metadata.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
if os.environ.get('GITHUB_OUTPUT'):
    with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8', newline='\n') as stream:
        for name, value in metadata.items():
            stream.write(f'{name}={value}\n')
print(f'Unsigned complete manager APK: {target} ({target.stat().st_size} bytes)')
print(json.dumps(metadata, sort_keys=True))
PY

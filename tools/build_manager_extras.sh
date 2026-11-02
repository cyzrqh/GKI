#!/usr/bin/env bash
# Restore official x86 LKMs and optional native SUSFS guard modules per KMI.
set -euo pipefail
tools_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
: "${GITHUB_WORKSPACE:?}" "${KMI:?}" "${DDK_IMAGE:?}" "${KSU_REF:?}" "${KSU_VARIANT:?}"
: "${MANAGER_PACKAGE:?}" "${MANAGER_CERT_SIZE:?}" "${MANAGER_CERT_SHA256:?}"
source_dir="$GITHUB_WORKSPACE/manager-source"
build_source="$GITHUB_WORKSPACE/full-native-source"
output="$GITHUB_WORKSPACE/manager-lkm/$KMI/extras"
test ! -e "$build_source"
python3 "$tools_dir/checkout_ksu_snapshot.py" --input "$GITHUB_WORKSPACE/ksu-source-bundle" \
  --output "$build_source" --expected-ref "$KSU_REF"
if [ "$KSU_VARIANT" = BakaSU ]; then
  # The native symbol lookup returns an address (unsigned long). Retain both
  # real x86 callbacks with their declared types, without relaxing -Werror.
  python3 - "$build_source/kernel/hook/x86_64/syscall_hook.c" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text(encoding='utf-8')
for name in ('syscall_enter_from_user_mode', 'syscall_exit_to_user_mode'):
    variable = name + '_fn'
    old = variable + ' = find_kernel_symbol_exact("' + name + '");'
    new = variable + ' = (typeof(' + variable + '))find_kernel_symbol_exact("' + name + '");'
    if text.count(old) == 1:
        text = text.replace(old, new, 1)
path.write_text(text, encoding='utf-8', newline='\n')
PY
fi
mkdir -p "$output"
version=$(python3 "$tools_dir/prepare_manager_lkm.py" version-code --source "$source_dir" --expected-ref "$KSU_REF")
abis=$(python3 "$tools_dir/manager_full_native.py" abis --source "$source_dir")
header="$build_source/.gki-extra-source.h"
cat > "$header" <<EOF
#ifndef __ASSEMBLY__
#include <linux/module.h>
MODULE_INFO(gki_extra_source_revision, "$KSU_REF");
#endif
EOF
docker pull "$DDK_IMAGE"
docker run --rm --network none --volume "$GITHUB_WORKSPACE:$GITHUB_WORKSPACE" \
  --workdir "$build_source/kernel" --entrypoint /bin/bash \
  --env HOME=/tmp --env KMI --env KSU_VARIANT --env MANAGER_PACKAGE --env MANAGER_CERT_SIZE \
  --env MANAGER_CERT_SHA256 --env KSU_VERSION="$version" --env NATIVE_ABIS="$abis" \
  --env FULL_NATIVE_OUTPUT="$output" --env SOURCE_HEADER="$header" \
  "$DDK_IMAGE" "$tools_dir/build_manager_extras_container.sh"
python3 "$tools_dir/manager_full_native.py" record --source "$source_dir" --directory "$output" \
  --identity "$GITHUB_WORKSPACE/manager-public-certificate.json" --expected-ref "$KSU_REF" \
  --kmi "$KMI" --ddk-image "$DDK_IMAGE"

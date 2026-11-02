#!/usr/bin/env bash
# Reuse each existing native job's immutable DDK for its NoMount component.
set -euo pipefail
: "${GITHUB_WORKSPACE:?}" "${KMI:?}" "${DDK_IMAGE:?}" "${KSU_REF:?}"
test "$KSU_VARIANT" = FinaSU
source_dir="$GITHUB_WORKSPACE/full-native-source"
output="$GITHUB_WORKSPACE/manager-lkm/$KMI/nomount"
mkdir -p "$output"
test -s "$source_dir/.gki-extra-source.h"
docker run --rm --network none --volume "$GITHUB_WORKSPACE:$GITHUB_WORKSPACE" \
  --workdir "$source_dir" --entrypoint /bin/sh --env HOME=/tmp \
  --env "KCFLAGS=-include $source_dir/.gki-extra-source.h" \
  --env "NOMOUNT_OUT=$output/${KMI}_nomount.ko" \
  "$DDK_IMAGE" userspace/nomount/build-lkm.sh
python3 "$GITHUB_WORKSPACE/tools/fina_native_components.py" record \
  --source "$GITHUB_WORKSPACE/manager-source" --directory "$output" \
  --identity "$GITHUB_WORKSPACE/manager-public-certificate.json" \
  --expected-ref "$KSU_REF" --kmi "$KMI" --ddk-image "$DDK_IMAGE"

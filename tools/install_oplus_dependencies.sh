#!/usr/bin/env bash
# Keep the native-worker entry point on the shared dependency recovery policy.
set -euo pipefail
exec bash "$(dirname "${BASH_SOURCE[0]}")/install_build_dependencies.sh" oplus

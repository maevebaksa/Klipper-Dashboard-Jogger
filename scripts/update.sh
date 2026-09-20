#!/usr/bin/env bash
set -euo pipefail
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
git -C "$SOURCE" pull --ff-only
bash "$SOURCE/scripts/install.sh"

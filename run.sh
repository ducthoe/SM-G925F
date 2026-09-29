#!/usr/bin/env bash
set -euo pipefail
project_root=$(cd -- "$(dirname -- "$0")" && pwd)
if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
    printf 'This version requires an x86_64 Linux PC.\n' >&2
    exit 1
fi
if ! command -v python3 >/dev/null; then
    for argument in "$@"; do
        if [[ $argument == --no-install ]]; then
            printf 'Python 3 is missing; install it or omit --no-install.\n' >&2
            exit 1
        fi
    done
    "$project_root/scripts/install-dependencies.sh"
fi
exec python3 "$project_root/scripts/bootstrap.py" "$@"

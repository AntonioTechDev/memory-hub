#!/usr/bin/env sh
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m memoryhub.install "$@"

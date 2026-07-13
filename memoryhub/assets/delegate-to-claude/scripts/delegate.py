#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def main() -> None:
    fallback = Path.home() / ".local" / "bin" / "memoryhub"
    binary = str(fallback) if fallback.is_file() else shutil.which("memoryhub")
    if binary is None:
        raise SystemExit("memoryhub is not installed; run the repository install.sh first")
    os.execv(binary, [binary, "delegate-claude", *sys.argv[1:]])


if __name__ == "__main__":
    main()

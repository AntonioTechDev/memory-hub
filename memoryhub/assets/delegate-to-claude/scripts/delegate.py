#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def main() -> None:
    binary = shutil.which("memoryhub")
    fallback = Path.home() / ".local" / "bin" / "memoryhub"
    if binary is None and fallback.is_file():
        binary = str(fallback)
    if binary is None:
        raise SystemExit("memoryhub is not installed; run the repository install.sh first")
    os.execv(binary, [binary, "delegate-claude", *sys.argv[1:]])


if __name__ == "__main__":
    main()

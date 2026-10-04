"""qqgate command line."""
from __future__ import annotations

import argparse

from qqgate import __version__


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="qqgate", description=__doc__)
    ap.add_argument("--version", action="version", version=f"qqgate {__version__}")
    ap.parse_args(argv)
    ap.print_help()
    return 0

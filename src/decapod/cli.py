"""Command-line interface (expanded in later milestones)."""

from __future__ import annotations

import argparse

from decapod import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="decapod")
    parser.add_argument("--version", action="version", version=f"decapod {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0

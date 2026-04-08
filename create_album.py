"""Backward-compatible wrapper for the unified album CLI."""

from __future__ import annotations

import sys

from onedrive_helper.cli import main as cli_main


def main() -> None:
    """Run the unified CLI in album mode."""
    sys.argv = [sys.argv[0], "album", *sys.argv[1:]]
    cli_main()


if __name__ == "__main__":
    main()

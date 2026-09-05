"""
Entry point module for the ``pepper`` CLI (console_scripts).
"""

from __future__ import annotations

import logging
import sys

from salt_pepper.cli import cli


def main() -> None:
    """Invoke the click CLI group."""
    try:
        cli()
    except Exception:
        logging.getLogger("pepper").debug(
            "Uncaught traceback:", exc_info=True
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

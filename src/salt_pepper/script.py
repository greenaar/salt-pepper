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
    except SystemExit:
        raise  # Click uses SystemExit — let it pass through
    except Exception:
        logging.getLogger("pepper").debug(
            "Uncaught traceback:", exc_info=True
        )
        import click as _click
        _click.secho(
            f"Error: {sys.exc_info()[1]}",
            err=True, fg="red",
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

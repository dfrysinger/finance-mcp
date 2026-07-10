"""Package entry point so ``python -m finance_mcp ...`` runs the CLI.

The launchd auto-sync agent invokes the CLI through the absolute interpreter
path (``sys.executable -m finance_mcp``) rather than the ``finance-mcp`` console
script, because a LaunchAgent runs with a minimal ``PATH`` that need not contain
the venv's ``bin`` directory. Routing through ``-m`` removes that PATH
dependency entirely.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

"""Allow ``python -m gita.cli`` to work when cli is a package."""
import sys

from gita.cli import main

sys.exit(main())

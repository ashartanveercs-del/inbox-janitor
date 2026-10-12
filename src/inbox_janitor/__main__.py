"""`python -m inbox_janitor ...` - the same as the `inbox-janitor` command."""

import sys

from .cli import main

sys.exit(main())

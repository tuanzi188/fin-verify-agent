"""`python -m evaluation.lint_assist_tasks` 的入口；规则在 `assist_schema.py`。"""

from __future__ import annotations

import sys

from evaluation.assist_schema import main

if __name__ == "__main__":
    sys.exit(main())

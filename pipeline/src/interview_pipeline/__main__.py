"""``python -m interview_pipeline`` の入口。"""

from __future__ import annotations

import sys

from interview_pipeline.cli import main

if __name__ == "__main__":
    sys.exit(main())

"""Make this chapter's modules importable as ``stack``, ``cost``, ``drill``, ``api``.

Same arrangement as the other chapters: this chapter's directories go to
the front of ``sys.path`` and any same-named module imported by an earlier
chapter's tests is forgotten before ours are collected.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "lambda", ROOT):
    if str(p) in sys.path:
        sys.path.remove(str(p))
    sys.path.insert(0, str(p))
for name in ("stack", "cost", "drill", "api"):
    sys.modules.pop(name, None)

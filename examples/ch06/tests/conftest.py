"""Make this chapter's modules importable as ``review`` and ``rollup``.

Same arrangement as the other chapters: this chapter's directory goes to
the front of ``sys.path`` and any same-named module imported by an earlier
chapter's tests is forgotten before ours are collected.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))
for name in ("review", "rollup"):
    sys.modules.pop(name, None)

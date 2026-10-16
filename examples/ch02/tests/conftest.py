"""Make this chapter's modules importable as ``stack``, ``cost``, ``ingest``...

Every chapter uses the same module names, so this chapter's directories go
to the front of ``sys.path`` and any ``stack``/``cost`` already imported by
an earlier chapter's tests is forgotten before ours are collected. Each test
module keeps a reference to the module it imported, so earlier chapters'
tests are unaffected.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "lambda", ROOT):
    if str(p) in sys.path:
        sys.path.remove(str(p))
    sys.path.insert(0, str(p))
for name in ("stack", "cost"):
    sys.modules.pop(name, None)

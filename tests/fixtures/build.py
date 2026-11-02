"""Write tests/fixtures/*.apns from tests/samples.py (run from the repo root: python tests/fixtures/build.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "unit"))
sys.path.insert(0, str(ROOT / "tests"))
import conftest  # noqa: E402,F401  (registers the fenton_home package without Home Assistant)
from test_payload import expected_fixtures  # noqa: E402

for name, payload in expected_fixtures().items():
    (ROOT / "tests" / "fixtures" / f"{name}.apns").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(name)

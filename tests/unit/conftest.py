"""Load the integration's pure modules (const, payload, apns) without importing Home Assistant."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "custom_components" / "fenton_home"

if "fenton_home" not in sys.modules:
    package = types.ModuleType("fenton_home")
    package.__path__ = [str(PACKAGE)]
    sys.modules["fenton_home"] = package
    for name in ("const", "entities", "snapshot", "model", "controls", "problems", "jobs", "slots", "engine", "live", "payload", "apns"):
        spec = importlib.util.spec_from_file_location(f"fenton_home.{name}", PACKAGE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

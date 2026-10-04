"""Read infra-config through its own loader and validator (qqcfg), from a checkout at a pinned commit.

The gate does not parse config itself: it imports `tools/qqcfg.py` from the checkout, refuses to
compute anything from a config that fails `qqcfg validate`, and then reads `qqcfg.load`.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from qqgate.errors import GateError


def qqcfg_module(root: Path) -> ModuleType:
    path = Path(root) / "tools" / "qqcfg.py"
    if not path.is_file():
        raise GateError(f"{root} is not an infra-config checkout: {path} is missing")
    spec = importlib.util.spec_from_file_location(f"qqcfg_{abs(hash(str(path.resolve())))}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses and friends look modules up by name
    spec.loader.exec_module(module)
    return module


def load(root: Path, validate: bool = True) -> dict:
    """Return infra-config as qqcfg.load returns it, after qqcfg validate passes."""
    qqcfg = qqcfg_module(root)
    if validate:
        errors, _ = qqcfg.validate(Path(root))
        if errors:
            raise GateError("infra-config fails qqcfg validate, so the gate refuses to use it:\n  "
                            + "\n  ".join(errors))
    try:
        return qqcfg.load(Path(root))
    except qqcfg.ConfigError as e:
        raise GateError(f"infra-config: {e}") from None

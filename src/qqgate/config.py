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
    name = f"qqcfg_{abs(hash(str(path.resolve())))}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise GateError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses and friends look modules up by name
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        del sys.modules[name]
        raise GateError(f"{path} failed to load: {type(e).__name__}: {e}") from None
    return module


def load(root: Path, validate: bool = True, code: Path | None = None) -> dict:
    """Return infra-config as qqcfg.load returns it, after qqcfg validate passes. `code` is the
    infra-config checkout whose qqcfg reads `root` when `root` holds only data (a user org's
    qq-config, `settings --infra-config`); by default `root` is that checkout itself."""
    qqcfg = qqcfg_module(code or root)
    if validate:
        errors, _ = qqcfg.validate(Path(root))
        if errors:
            raise GateError("infra-config fails qqcfg validate, so the gate refuses to use it:\n  "
                            + "\n  ".join(errors))
    try:
        return qqcfg.load(Path(root))
    except qqcfg.ConfigError as e:
        raise GateError(f"infra-config: {e}") from None

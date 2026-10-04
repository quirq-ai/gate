"""Backend-specific gate code, one module per backend named in infra-config (org.toml [[backend]]).

`load(name)` imports `qqgate.backends.<name>`; nothing registers backends, so `launchpad` is one
new module. Each backend module provides:
  check_workflows(cfg, required)   problems with how the backend runs each required check
  required_checks_rule(required)   the backend's rule that makes those checks required
  observe(repo_source, sha, token) check name -> conclusion for one commit
"""
from __future__ import annotations

import importlib
from types import ModuleType

from qqgate.errors import GateError


def load(name: str) -> ModuleType:
    if not name.isidentifier():
        raise GateError(f"bad backend name {name!r}")
    try:
        return importlib.import_module(f"qqgate.backends.{name}")
    except ModuleNotFoundError as e:
        if e.name == f"qqgate.backends.{name}":
            raise GateError(f"no gate backend {name!r} (qqgate/backends/{name}.py)") from None
        raise

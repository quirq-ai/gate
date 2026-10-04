import copy
import os
from pathlib import Path

import pytest

from qqgate import config

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def infra_config_root() -> Path:
    """The infra-config checkout at pins.toml's commit. CI checks it out to .qq/infra-config."""
    root = Path(os.environ.get("QQ_INFRA_CONFIG", ROOT / ".qq" / "infra-config"))
    if not (root / "tools" / "qqcfg.py").is_file():
        pytest.fail(f"no infra-config checkout at {root}; clone the pins.toml commit there or set QQ_INFRA_CONFIG")
    return root


@pytest.fixture(scope="session")
def config_root() -> Path:
    return infra_config_root()


@pytest.fixture(scope="session")
def _cfg(config_root):
    return config.load(config_root)


@pytest.fixture
def cfg(_cfg):
    return copy.deepcopy(_cfg)

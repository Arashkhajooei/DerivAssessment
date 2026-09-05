"""Loading and hashing of ``config.yaml``.

The config is hashed (not just loaded) because the run manifest records
that hash: a recommendation is only reproducible if you know which settings
produced it, not just which code did.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict

import yaml

from evalharness.errors import ConfigError

_REQUIRED_TOP_LEVEL = (
    "paths",
    "retrieval",
    "matching",
    "grounding",
    "risk",
    "gates",
    "aggregation",
    "judge",
)


def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    """Load ``config.yaml`` and check that the sections the code depends on
    are present. Raises ``ConfigError`` with a specific missing key rather
    than letting a later stage fail with a confusing ``KeyError``.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError("config file not found: {}".format(path))

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError("config file is not valid YAML: {}".format(exc)) from exc

    if not isinstance(raw, dict):
        raise ConfigError("config file must contain a mapping at the top level")

    missing = [key for key in _REQUIRED_TOP_LEVEL if key not in raw]
    if missing:
        raise ConfigError(
            "config file is missing required section(s): {}".format(", ".join(missing))
        )

    return raw


def config_hash(config: Dict[str, Any]) -> str:
    """Stable content hash of the config, for the run manifest.

    Keys are sorted so that key order in the YAML file (which carries no
    semantic meaning) cannot change the hash.
    """
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

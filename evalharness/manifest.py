"""Run manifest: provenance metadata written alongside the generated
artifacts so any recommendation can be traced back to the exact code,
config, and inputs that produced it -- what "reproducibility" concretely
means for this harness.
"""

from __future__ import annotations

import platform
import subprocess
from datetime import datetime, timezone
from typing import Dict, Optional

from evalharness.config import config_hash
from evalharness.hashing import sha256_file


def _git_commit() -> Optional[str]:
    """Best-effort current commit SHA. Returns None outside a git
    checkout or if git itself is unavailable -- this is provenance
    metadata, not a hard requirement, so its absence should never break
    the pipeline.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def build_run_manifest(
    config: dict,
    input_paths: Dict[str, str],
    counts: Dict[str, int],
    judge_backend: str,
    stage_timings_seconds: Dict[str, float],
) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "config_hash": config_hash(config),
        "input_hashes": {name: sha256_file(path) for name, path in input_paths.items()},
        "python_version": platform.python_version(),
        "judge": {
            "model": config["judge"]["model"],
            "backend_order": config["judge"]["backend_order"],
            "backend_used": judge_backend,
        },
        "counts": counts,
        "stage_timings_seconds": {k: round(v, 4) for k, v in stage_timings_seconds.items()},
    }

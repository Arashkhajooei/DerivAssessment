"""Read/write helpers for llm_calls.jsonl -- the append-only audit log of
every LLM call attempt, including cache replays and stub fallbacks.

The log is deliberately append-only and line-oriented (JSONL): every run
adds new entries, never rewrites old ones, so the file is a full history
of every judge invocation across every run, not just the most recent one.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List


def read_call_log(path: str) -> List[Dict]:
    """Read existing log entries. A missing file means "no history yet",
    not an error. A corrupted line is skipped rather than aborting the
    whole read -- it simply won't be available for a cache lookup.
    """
    if not os.path.exists(path):
        return []
    entries: List[Dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def append_call_log(path: str, entries: List[Dict]) -> None:
    if not entries:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")

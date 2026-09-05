#!/usr/bin/env python3
"""Local dashboard for the evaluation harness (FastAPI + uvicorn).

A small local server -- LOCAL USE ONLY, binds to 127.0.0.1 -- that lets a
tester:

  * see every pipeline stage and artifact in one page (retrieval, rule
    checks, judge output, failure taxonomy, recommendation, review
    report, run manifest, and the raw LLM call log)
  * trigger `run.py`, `validate.py`, and the test suite from the browser
  * upload a different kb.json / queries.json / candidate_answers.json
    and re-run against it, to exercise the fixture-swap claim directly
  * restore the original committed sample fixture afterwards

This is deliberately NOT part of the graded harness. It is a thin,
optional viewer layered on top: its dependencies (FastAPI, uvicorn,
python-multipart) live in requirements-frontend.txt and the harness
itself needs none of them -- `run.py` / `validate.py` behave identically
whether or not this is ever started.

Security note: this server executes local subprocesses (run.py,
validate.py, pytest) on request, so it binds to loopback only and runs
without auto-reload. It is a developer tool, not something to expose
beyond localhost.

Run it with:  .venv/bin/python frontend/server.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evalharness.aggregate import compute_recommendation  # noqa: E402
from evalharness.config import load_config  # noqa: E402
from evalharness.errors import InputValidationError  # noqa: E402
from evalharness.hashing import sha256_file  # noqa: E402
from evalharness.loader import load_inputs  # noqa: E402
from evalharness.schemas import (  # noqa: E402
    AutomatedScoreRecord,
    FailureTaxonomyRecord,
    LLMReviewRecord,
)

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
BACKUP_DIR = HERE / "sample_backup"
UPLOAD_TMP = HERE / "_upload_tmp"
FIXTURE_FILES = ("kb.json", "queries.json", "candidate_answers.json")

# Far larger than any plausible fixture; it just stops an enormous
# upload from being buffered into memory.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

app = FastAPI(title="Evaluation Harness Dashboard", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _ensure_backup() -> None:
    """Snapshot the original sample fixture on first start, so a tester
    who uploads their own data can always get back to the committed
    sample with one click.
    """
    BACKUP_DIR.mkdir(exist_ok=True)
    for name in FIXTURE_FILES:
        backup_path = BACKUP_DIR / name
        source = ROOT / name
        if not backup_path.exists() and source.exists():
            shutil.copyfile(source, backup_path)


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _read_text(path: Path) -> Optional[str]:
    return path.read_text(encoding="utf-8") if path.exists() else None


def _read_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


# Tools like pytest emit ANSI colour codes when they think a terminal is
# attached. Those render as literal escape-sequence garbage in the
# browser's <pre>, so strip them on the way out.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _clean(text: Optional[str]) -> str:
    return _ANSI_RE.sub("", text or "")


def _run_subprocess(args: List[str], timeout: int = 120) -> Dict[str, Any]:
    """Run a project command with the same interpreter serving this app,
    so it always has the same installed dependencies.
    """
    try:
        result = subprocess.run(
            [sys.executable] + args,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "success": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": _clean(result.stdout),
            "stderr": _clean(result.stderr),
            "command": " ".join(["python"] + args),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "exit_code": None,
            "stdout": _clean(exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout),
            "stderr": _clean(exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr)
            + "\n\nTIMED OUT after {}s".format(timeout),
            "command": " ".join(["python"] + args),
        }


# GET and HEAD: health checks and preview harnesses commonly probe with
# HEAD, which would otherwise return 405 here.
@app.api_route("/", methods=["GET", "HEAD"])
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/state")
def api_state() -> Dict[str, Any]:
    """Everything the dashboard needs, in one payload.

    The recommendation is *recomputed* from the artifacts on disk via the
    same `compute_recommendation` the pipeline uses, rather than scraped
    out of recommendation.md -- so the dashboard can never show a
    differently-derived answer than the harness itself would.
    """
    config = load_config(str(ROOT / "config.yaml"))
    paths = {k: ROOT / v for k, v in config["paths"].items()}

    state: Dict[str, Any] = {
        "has_results": False,
        "using_custom_fixture": False,
        "input_error": None,
        "input_counts": None,
        "kb": None,
        "queries": None,
        "answers": None,
        "manifest": None,
        "retrieval": None,
        "automated_scores": None,
        "llm_review": None,
        "failure_taxonomy": None,
        "llm_calls": [],
        "recommendation": None,
        "recommendation_markdown": None,
        "review_report_markdown": None,
    }

    _ensure_backup()
    try:
        current = {n: sha256_file(str(ROOT / n)) for n in FIXTURE_FILES}
        backup = {n: sha256_file(str(BACKUP_DIR / n)) for n in FIXTURE_FILES}
        state["using_custom_fixture"] = current != backup
    except OSError:
        pass

    # Input validation failures are surfaced in the UI exactly as the
    # harness reports them, rather than as a generic server error.
    try:
        inputs = load_inputs(
            str(paths["kb"]), str(paths["queries"]), str(paths["candidate_answers"])
        )
    except InputValidationError as exc:
        state["input_error"] = [issue.render() for issue in exc.issues]
        return state

    state["input_counts"] = {
        "kb_docs": len(inputs.kb),
        "queries": len(inputs.queries),
        "variants": len(inputs.variant_names),
        "variant_names": list(inputs.variant_names),
    }
    state["kb"] = [d.model_dump() for d in inputs.kb]
    state["queries"] = [q.model_dump() for q in inputs.queries]
    state["answers"] = {a.query_id: a.answers for a in inputs.answers}

    state["manifest"] = _read_json(paths["run_manifest"])
    state["retrieval"] = _read_json(paths["retrieval"])
    state["automated_scores"] = _read_json(paths["automated_scores"])
    state["llm_review"] = _read_json(paths["llm_review"])
    state["failure_taxonomy"] = _read_json(paths["failure_taxonomy"])
    state["llm_calls"] = _read_jsonl(paths["llm_calls"])
    state["recommendation_markdown"] = _read_text(paths["recommendation"])
    state["review_report_markdown"] = _read_text(paths["review_report"])

    if state["automated_scores"] and state["llm_review"] and state["failure_taxonomy"] is not None:
        try:
            scores = [AutomatedScoreRecord(**r) for r in state["automated_scores"]]
            reviews = [LLMReviewRecord(**r) for r in state["llm_review"]]
            taxonomy = [FailureTaxonomyRecord(**r) for r in state["failure_taxonomy"]]
            rec = compute_recommendation(inputs.queries, scores, reviews, taxonomy, config)
            state["recommendation"] = {
                "selected_variant": rec.selected_variant,
                "reasons": rec.reasons,
                "tradeoffs": rec.tradeoffs,
                "margin": rec.margin,
                "verdicts": [
                    {
                        "variant": v.variant,
                        "disqualified": v.disqualified,
                        "disqualifying_reasons": v.disqualifying_reasons,
                        "composite_score": v.composite_score,
                        "mean_clarity": v.mean_clarity,
                        "mean_faithfulness": v.mean_faithfulness,
                        "judge_wins": v.judge_wins,
                        "failure_tag_counts": v.failure_tag_counts,
                    }
                    for v in rec.verdicts
                ],
            }
            state["has_results"] = True
        except Exception as exc:  # noqa: BLE001 -- surface it, don't 500 the dashboard
            state["input_error"] = ["Could not recompute recommendation: {}".format(exc)]

    return state


@app.post("/api/run")
def api_run() -> Dict[str, Any]:
    return _run_subprocess(["run.py"])


@app.post("/api/validate")
def api_validate() -> Dict[str, Any]:
    return _run_subprocess(["validate.py"])


@app.post("/api/test")
def api_test() -> Dict[str, Any]:
    return _run_subprocess(["-m", "pytest", "-q"])


@app.post("/api/upload-fixture")
async def api_upload_fixture(
    kb: UploadFile = File(...),
    queries: UploadFile = File(...),
    answers: UploadFile = File(...),
):
    """Replace the three input files with an uploaded set.

    The uploads are written to a temp directory and run through the
    harness's own `load_inputs` FIRST. Only if they pass every schema and
    referential-integrity check are they copied into place -- so a
    malformed upload can never clobber a working fixture, and the tester
    sees the exact same validation errors the pipeline would produce.
    """
    incoming = {"kb.json": kb, "queries.json": queries, "candidate_answers.json": answers}

    UPLOAD_TMP.mkdir(exist_ok=True)
    try:
        for filename, upload in incoming.items():
            content = await upload.read()
            if len(content) > MAX_UPLOAD_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={
                        "success": False,
                        "errors": [
                            "{} is larger than the {} MB upload limit".format(
                                filename, MAX_UPLOAD_BYTES // (1024 * 1024)
                            )
                        ],
                    },
                )
            (UPLOAD_TMP / filename).write_bytes(content)

        try:
            load_inputs(
                str(UPLOAD_TMP / "kb.json"),
                str(UPLOAD_TMP / "queries.json"),
                str(UPLOAD_TMP / "candidate_answers.json"),
            )
        except InputValidationError as exc:
            # Rewrite the temp-directory prefix out of the messages: the
            # tester uploaded "queries.json", and seeing a server-side
            # scratch path in the error is noise (and leaks local layout).
            prefix = str(UPLOAD_TMP) + "/"
            errors = [i.render().replace(prefix, "") for i in exc.issues]
            return JSONResponse(status_code=422, content={"success": False, "errors": errors})

        for filename in incoming:
            shutil.copyfile(str(UPLOAD_TMP / filename), str(ROOT / filename))
        return {"success": True}
    finally:
        shutil.rmtree(UPLOAD_TMP, ignore_errors=True)


@app.post("/api/restore-sample")
def api_restore_sample() -> Dict[str, Any]:
    _ensure_backup()
    for name in FIXTURE_FILES:
        backup = BACKUP_DIR / name
        if backup.exists():
            shutil.copyfile(str(backup), str(ROOT / name))
    return {"success": True}


if __name__ == "__main__":
    import uvicorn

    _ensure_backup()
    print("Evaluation harness dashboard: http://127.0.0.1:5050  (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=5050, log_level="info")

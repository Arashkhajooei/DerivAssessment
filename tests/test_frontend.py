"""Tests for the optional dashboard (`frontend/server.py`).

These are skipped entirely when FastAPI is not installed, because the
dashboard's dependencies live in `requirements-frontend.txt` and the
harness itself does not require them -- someone who installed only
`requirements-dev.txt` should get a clean run, not failures.

Two deliberate choices about what is *not* exercised here:

  * `POST /api/test` runs `pytest` as a subprocess. Calling it from
    within a test would spawn a suite that spawns a suite, recursing
    without end. Its wiring is tested with the subprocess call mocked.
  * `POST /api/run` regenerates artifacts and appends to
    `llm_calls.jsonl`, which would leave the working tree dirty after a
    test run. It is also tested with the subprocess mocked.

`POST /api/validate` *is* called for real: it only reads, so it has no
side effects, and it proves the real subprocess path works end to end.
"""

import json
import shutil
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="dashboard extras not installed")
pytest.importorskip("httpx", reason="fastapi TestClient requires httpx")

from fastapi.testclient import TestClient  # noqa: E402

import frontend.server as server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_FILES = ("kb.json", "queries.json", "candidate_answers.json")


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def preserve_inputs(tmp_path):
    """Snapshot the three input files and restore them afterwards.

    Upload tests write to the real repository files, so restoration has
    to happen even if the test fails -- hence a fixture with teardown
    rather than cleanup at the end of the test body.
    """
    saved = {}
    for name in FIXTURE_FILES:
        saved[name] = (ROOT / name).read_bytes()
    yield
    for name, content in saved.items():
        (ROOT / name).write_bytes(content)


class TestPageAndState:
    def test_index_serves_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_head_request_is_supported(self, client):
        # Health checks and preview harnesses probe with HEAD; without an
        # explicit route this returned 405.
        assert client.head("/").status_code == 200

    def test_state_returns_every_expected_key(self, client):
        payload = client.get("/api/state").json()
        for key in (
            "has_results", "using_custom_fixture", "input_error", "input_counts",
            "kb", "queries", "answers", "judge_config", "manifest", "retrieval",
            "automated_scores", "llm_review", "failure_taxonomy", "llm_calls",
            "recommendation", "recommendation_markdown", "review_report_markdown",
        ):
            assert key in payload, "missing key: {}".format(key)

    def test_state_counts_match_the_input_files(self, client):
        payload = client.get("/api/state").json()
        kb = json.loads((ROOT / "kb.json").read_text())
        queries = json.loads((ROOT / "queries.json").read_text())
        answers = json.loads((ROOT / "candidate_answers.json").read_text())
        variants = sorted({v for a in answers for v in a["answers"]})

        assert len(payload["kb"]) == len(kb)
        assert len(payload["queries"]) == len(queries)
        assert payload["input_counts"]["variants"] == len(variants)

    def test_state_reports_the_judge_backend_actually_used(self, client):
        # The UI shows this next to the verdict so a stub run is never
        # mistaken for real model judgment; it must be present and honest.
        payload = client.get("/api/state").json()
        assert payload["manifest"]["judge"]["backend_used"] in {"live", "cache", "stub"}

    def test_state_recommendation_matches_the_pipeline(self, client):
        """The dashboard imports compute_recommendation rather than parsing
        recommendation.md, so it cannot display a verdict the harness
        would not reach. Verify the two agree.
        """
        payload = client.get("/api/state").json()
        selected = payload["recommendation"]["selected_variant"]
        markdown = (ROOT / "recommendation.md").read_text()
        if selected:
            assert "`{}`".format(selected) in markdown


class TestCommandEndpoints:
    def test_validate_runs_for_real_and_passes(self, client):
        # validate.py only reads, so this is safe to invoke.
        body = client.post("/api/validate").json()
        assert body["exit_code"] == 0
        assert "VALIDATION PASSED" in body["stdout"]
        assert body["command"] == "python validate.py"

    def test_run_endpoint_invokes_run_py(self, client, monkeypatch):
        captured = {}

        def fake(args, timeout=120):
            captured["args"] = args
            return {"success": True, "exit_code": 0, "stdout": "ok",
                    "stderr": "", "command": "python " + " ".join(args)}

        monkeypatch.setattr(server, "_run_subprocess", fake)
        body = client.post("/api/run").json()
        assert captured["args"] == ["run.py"]
        assert body["exit_code"] == 0

    def test_test_endpoint_invokes_pytest(self, client, monkeypatch):
        # Mocked deliberately: really calling this would recurse.
        captured = {}

        def fake(args, timeout=120):
            captured["args"] = args
            return {"success": True, "exit_code": 0, "stdout": "ok",
                    "stderr": "", "command": "python " + " ".join(args)}

        monkeypatch.setattr(server, "_run_subprocess", fake)
        client.post("/api/test")
        assert "pytest" in " ".join(captured["args"])

    def test_subprocess_output_is_stripped_of_ansi_codes(self):
        # pytest emits colour escapes when it thinks a terminal is
        # attached; those render as garbage in the browser's <pre>.
        assert server._clean("\x1b[31mred\x1b[0m text") == "red text"
        assert server._clean(None) == ""


class TestFixtureUpload:
    def _upload(self, client, kb, queries, answers):
        return client.post(
            "/api/upload-fixture",
            files={
                "kb": ("kb.json", kb, "application/json"),
                "queries": ("queries.json", queries, "application/json"),
                "answers": ("candidate_answers.json", answers, "application/json"),
            },
        )

    def test_invalid_fixture_is_rejected_and_files_are_untouched(self, client, preserve_inputs):
        """The safety property that matters most: a fixture that fails
        validation must not overwrite the working one.
        """
        before = {n: (ROOT / n).read_bytes() for n in FIXTURE_FILES}

        queries = json.loads((ROOT / "queries.json").read_text())
        queries[0]["expected_doc_ids"] = ["D_DOES_NOT_EXIST"]

        r = self._upload(
            client,
            (ROOT / "kb.json").read_text(),
            json.dumps(queries),
            (ROOT / "candidate_answers.json").read_text(),
        )
        body = r.json()

        assert body["success"] is False
        assert any("DANGLING_DOC_ID" in e for e in body["errors"])
        # nothing on disk changed
        for name in FIXTURE_FILES:
            assert (ROOT / name).read_bytes() == before[name]

    def test_malformed_json_is_rejected_without_writing(self, client, preserve_inputs):
        before = (ROOT / "kb.json").read_bytes()
        r = self._upload(
            client,
            "{ this is not valid json",
            (ROOT / "queries.json").read_text(),
            (ROOT / "candidate_answers.json").read_text(),
        )
        assert r.json()["success"] is False
        assert (ROOT / "kb.json").read_bytes() == before

    def test_valid_three_variant_fixture_is_accepted(self, client, preserve_inputs):
        """A structurally different fixture -- different ids, three
        variants -- must be accepted, proving the upload path is not
        tied to the sample data's shape.
        """
        d = ROOT / "tests" / "fixtures" / "broken"
        r = self._upload(
            client,
            (d / "good_3variant_kb.json").read_text(),
            (d / "good_3variant_queries.json").read_text(),
            (d / "good_3variant_answers.json").read_text(),
        )
        assert r.json()["success"] is True

        state = client.get("/api/state").json()
        assert state["using_custom_fixture"] is True
        assert state["input_counts"]["variants"] == 3

    def test_restore_sample_puts_the_originals_back(self, client, preserve_inputs):
        d = ROOT / "tests" / "fixtures" / "broken"
        self._upload(
            client,
            (d / "good_3variant_kb.json").read_text(),
            (d / "good_3variant_queries.json").read_text(),
            (d / "good_3variant_answers.json").read_text(),
        )
        assert client.get("/api/state").json()["using_custom_fixture"] is True

        assert client.post("/api/restore-sample").json()["success"] is True
        assert client.get("/api/state").json()["using_custom_fixture"] is False


class TestInputErrorHandling:
    def test_broken_inputs_surface_as_a_message_not_a_500(self, client, preserve_inputs):
        """If the inputs on disk are invalid, the dashboard must report
        the harness's own errors rather than returning a server error.
        """
        (ROOT / "queries.json").write_text("[]")  # no queries at all

        r = client.get("/api/state")
        assert r.status_code == 200
        payload = r.json()
        assert payload["input_error"] is not None

/* Dashboard logic. Plain ES modules-free JS -- no framework, no build
   step. All rendering is driven from one /api/state payload so the page
   can never show a partially-refreshed mix of two different runs. */

let state = null;

/* ---------- small helpers ---------- */

const $ = (sel) => document.querySelector(sel);

function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function passBadge(ok, labelTrue, labelFalse) {
  const cls = ok ? "badge-pass" : "badge-fail";
  const text = ok ? (labelTrue || "pass") : (labelFalse || "FAIL");
  return `<span class="badge ${cls}">${esc(text)}</span>`;
}

function riskBadge(level) {
  const map = { high: "badge-fail", critical: "badge-fail", medium: "badge-warn" };
  return `<span class="badge ${map[level] || "badge-neutral"}">${esc(level)} risk</span>`;
}

/* ---------- console panel ---------- */

function showConsole(title, result) {
  $("#console-panel").classList.remove("hidden");
  $("#console-title").textContent = title;

  const status = $("#console-status");
  if (result.success) {
    status.className = "badge badge-pass";
    status.textContent = "exit " + result.exit_code;
  } else {
    status.className = "badge badge-fail";
    status.textContent = result.exit_code === null ? "timed out" : "exit " + result.exit_code;
  }

  const body = [];
  if (result.command) body.push("$ " + result.command + "\n");
  if (result.stdout) body.push(result.stdout);
  if (result.stderr) body.push(result.stderr);
  $("#console-body").textContent = body.join("\n").trim() || "(no output)";
}

function setBusy(busy, activeBtn, label) {
  ["#btn-run", "#btn-validate", "#btn-test"].forEach((id) => { $(id).disabled = busy; });
  if (activeBtn) $(activeBtn).textContent = busy ? label + "…" : label;
}

async function runCommand(endpoint, btn, label, title) {
  setBusy(true, btn, label);
  try {
    const res = await fetch(endpoint, { method: "POST" });
    showConsole(title, await res.json());
  } catch (err) {
    showConsole(title, { success: false, exit_code: null, stderr: String(err) });
  } finally {
    setBusy(false, btn, label);
    await refresh();
  }
}

/* ---------- state load + render ---------- */

async function refresh() {
  const res = await fetch("/api/state");
  state = await res.json();
  renderBanner();
  renderFixtureBadge();
  renderOverview();
  renderQueries();
  renderVariants();
  renderLogs();
  renderFixture();
}

function renderBanner() {
  const banner = $("#banner");
  if (!state.input_error) { banner.classList.add("hidden"); return; }
  banner.classList.remove("hidden");
  banner.innerHTML =
    "<strong>Input validation failed — the pipeline cannot run against the current files.</strong><ul>" +
    state.input_error.map((e) => `<li><code>${esc(e)}</code></li>`).join("") + "</ul>";
}

function renderFixtureBadge() {
  const badge = $("#fixture-badge");
  if (state.using_custom_fixture) {
    badge.className = "badge badge-warn";
    badge.textContent = "custom fixture loaded";
  } else {
    badge.className = "badge badge-neutral";
    badge.textContent = "original sample";
  }
}

function emptyState(message) {
  return `<div class="empty-state"><strong>No results yet</strong>${esc(message)}</div>`;
}


/* Which judge backend actually served the current results? A stub run
   must never be mistaken for a real model judgment, so this is shown
   next to the verdict itself, not buried in the logs tab. */
function judgeBackendBadge() {
  const used = state.manifest && state.manifest.judge && state.manifest.judge.backend_used;
  const cfg = state.judge_config || {};
  const model = (state.manifest && state.manifest.judge && state.manifest.judge.model) || cfg.model;
  const provider = (state.manifest && state.manifest.judge && state.manifest.judge.provider) || cfg.provider;
  if (!used) return '<span class="badge badge-neutral">unknown</span>';
  const cls = used === "stub" ? "badge-warn" : "badge-pass";
  const detail = used === "stub" ? "rule-derived, not judged" : `${esc(provider)}/${esc(model)}`;
  return `<span class="badge ${cls}">${esc(used)}</span> <span class="hint">${detail}</span>`;
}

/* ---------- tab: overview ---------- */

function renderOverview() {
  const el = $("#tab-overview");
  if (!state.has_results || !state.recommendation) {
    el.innerHTML = emptyState('Click "Run Pipeline" above to generate the artifacts.');
    return;
  }
  const rec = state.recommendation;
  const promoted = rec.selected_variant;
  const counts = state.input_counts || {};

  let html = `
    <div class="verdict ${promoted ? "promote" : "no-promote"}">
      <div>
        <div class="verdict-label">Recommended for promotion</div>
        <div class="verdict-value">${promoted ? esc(promoted) : "NO PROMOTION"}</div>
      </div>
      <div style="flex:1"></div>
      <div>
        <div class="verdict-label">Evaluated</div>
        <div>${counts.queries || 0} queries &times; ${counts.variants || 0} variants</div>
      </div>
      ${rec.margin !== null && rec.margin !== undefined
        ? `<div><div class="verdict-label">Composite margin</div><div class="mono">${esc(rec.margin)}</div></div>` : ""}
      <div>
        <div class="verdict-label">Judge</div>
        <div>${judgeBackendBadge()}</div>
      </div>
    </div>

    <div class="card">
      <h2>Why</h2>
      <ul class="reason-list">${rec.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      ${rec.tradeoffs.length ? `<h3>Tradeoffs</h3><ul class="reason-list">${
        rec.tradeoffs.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : ""}
    </div>`;

  if (state.recommendation_markdown) {
    html += `<div class="card"><h2>recommendation.md</h2><details><summary>show raw file</summary><pre>${
      esc(state.recommendation_markdown)}</pre></details></div>`;
  }
  if (state.review_report_markdown) {
    html += `<div class="card"><h2>review_report.md</h2><details><summary>show raw file</summary><pre>${
      esc(state.review_report_markdown)}</pre></details></div>`;
  }
  el.innerHTML = html;
}

/* ---------- tab: per-query ---------- */

function renderQueries() {
  const el = $("#tab-queries");
  if (!state.queries) { el.innerHTML = emptyState("Load a valid fixture first."); return; }
  if (!state.has_results) {
    el.innerHTML = emptyState('Click "Run Pipeline" to score these queries.');
    return;
  }

  const retrievalBy = {};
  (state.retrieval || []).forEach((r) => { retrievalBy[r.query_id] = r; });
  const reviewBy = {};
  (state.llm_review || []).forEach((r) => { reviewBy[r.query_id] = r; });
  const scoresBy = {};
  (state.automated_scores || []).forEach((s) => { scoresBy[s.query_id + "|" + s.variant] = s; });
  const tagsBy = {};
  (state.failure_taxonomy || []).forEach((t) => { tagsBy[t.query_id + "|" + t.variant] = t.tags; });

  el.innerHTML = state.queries.map((q) => {
    const retrieval = retrievalBy[q.query_id];
    const review = reviewBy[q.query_id];
    const answers = (state.answers || {})[q.query_id] || {};
    const variants = Object.keys(answers).sort();

    const evidenceHtml = retrieval && retrieval.retrieved.length
      ? retrieval.retrieved.map((p) => `
          <div class="evidence">
            <div class="evidence-head">
              <span class="doc-id">${esc(p.doc_id)}</span>
              <span class="badge badge-neutral">score ${esc(p.score)}</span>
              <span>${esc(p.title)}</span>
              ${(q.expected_doc_ids || []).includes(p.doc_id)
                ? '<span class="badge badge-pass">expected</span>' : ""}
            </div>
            <p class="evidence-text">${esc(p.text)}</p>
          </div>`).join("")
      : "<p class='hint'>No evidence retrieved.</p>";

    const variantsHtml = variants.map((v) => {
      const s = scoresBy[q.query_id + "|" + v];
      const tags = tagsBy[q.query_id + "|" + v] || [];
      const isWinner = review && review.winner === v;
      if (!s) return "";
      return `
        <div class="variant-block ${isWinner ? "is-winner" : ""}">
          <div class="check-row">
            <span class="variant-name">${esc(v)}</span>
            ${isWinner ? '<span class="badge badge-pass">judge winner</span>' : ""}
          </div>
          <p class="variant-answer">${esc(answers[v])}</p>
          <div class="check-row">
            ${passBadge(s.retrieval_hit, "retrieval hit", "retrieval MISS")}
            ${passBadge(s.must_include_pass, "must-include", "must-include FAIL")}
            ${passBadge(s.must_not_claim_pass, "no banned claim", "BANNED CLAIM")}
            <span class="badge badge-neutral">grounding ${esc(s.grounding_score)}</span>
          </div>
          ${tags.length ? `<div class="tag-row">${tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>` : ""}
          <details><summary>check details</summary><pre>${esc(s.notes)}</pre></details>
        </div>`;
    }).join("");

    const judgeHtml = review ? `
      <div class="judge-box">
        <strong>LLM judge</strong> — winner: <span class="mono">${esc(review.winner)}</span>
        <div class="check-row" style="margin-top:6px">
          ${variants.map((v) => `<span class="badge badge-neutral">${esc(v)}: faith ${
            esc(review.faithfulness[v])} / clarity ${esc(review.clarity[v])}${
            review.overclaim_flags[v] ? " / overclaim" : ""}</span>`).join("")}
        </div>
        <p class="justification">${esc(review.justification)}</p>
      </div>` : "";

    return `
      <div class="query-card">
        <div class="query-head">
          <span class="qid">${esc(q.query_id)}</span>${riskBadge(q.risk_level)}
          <p class="query-question">${esc(q.user_question)}</p>
        </div>
        <div class="query-body">
          <h3>Retrieved evidence (top ${retrieval ? retrieval.retrieved.length : 0})</h3>
          ${evidenceHtml}
          <h3>Candidate answers</h3>
          ${variantsHtml}
          ${judgeHtml}
          <details>
            <summary>constraints for this query</summary>
            <pre>${esc(JSON.stringify({
              expected_doc_ids: q.expected_doc_ids,
              must_include_any: q.must_include_any,
              must_not_claim: q.must_not_claim,
            }, null, 2))}</pre>
          </details>
        </div>
      </div>`;
  }).join("");
}

/* ---------- tab: variants ---------- */

function renderVariants() {
  const el = $("#tab-variants");
  if (!state.has_results || !state.recommendation) {
    el.innerHTML = emptyState('Click "Run Pipeline" to compare variants.');
    return;
  }
  const rows = state.recommendation.verdicts.map((v) => {
    const tags = Object.entries(v.failure_tag_counts || {})
      .sort().map(([k, n]) => `<span class="tag">${esc(k)}&times;${n}</span>`).join(" ") || "—";
    return `
      <tr class="${v.disqualified ? "disqualified" : ""}">
        <td class="mono"><strong>${esc(v.variant)}</strong></td>
        <td>${v.disqualified
          ? `<span class="badge badge-fail">DISQUALIFIED</span><div class="hint">${
              v.disqualifying_reasons.map(esc).join("<br>")}</div>`
          : '<span class="badge badge-pass">passes safety gate</span>'}</td>
        <td class="mono">${esc(v.composite_score)}</td>
        <td class="mono">${v.mean_faithfulness == null ? "—" : esc(v.mean_faithfulness)}</td>
        <td class="mono">${v.mean_clarity == null ? "—" : esc(v.mean_clarity)}</td>
        <td class="mono">${esc(v.judge_wins)}</td>
        <td>${tags}</td>
      </tr>`;
  }).join("");

  el.innerHTML = `
    <div class="card">
      <h2>Variant comparison</h2>
      <div class="table-scroll">
        <table>
          <thead><tr>
            <th>Variant</th><th>Safety gate</th><th>Composite</th>
            <th>Judge faithfulness</th><th>Judge clarity</th><th>Judge wins</th><th>Failure tags</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <p class="hint">The safety gate is a veto: a disqualified variant cannot be promoted
      regardless of composite score.</p>
    </div>`;
}

/* ---------- tab: logs ---------- */

function renderLogs() {
  const el = $("#tab-logs");
  const calls = state.llm_calls || [];

  const callRows = calls.length ? calls.map((c) => `
      <tr>
        <td><span class="badge ${c.backend === "live" ? "badge-warn"
          : c.backend === "cache" ? "badge-pass" : "badge-neutral"}">${esc(c.backend)}</span></td>
        <td>${passBadge(c.parsed_ok, "ok", "not used")}</td>
        <td class="mono">${esc((c.timestamp || "").replace("T", " ").slice(0, 19))}</td>
        <td class="mono">${esc(c.model || "—")}</td>
        <td class="mono">${esc((c.prompt_hash || "").slice(0, 12))}…</td>
        <td>${(c.validation_errors || []).length
          ? `<span class="hint">${esc(c.validation_errors.join("; "))}</span>` : "—"}</td>
      </tr>`).join("")
    : `<tr><td colspan="6" class="hint">No LLM calls logged yet.</td></tr>`;

  const m = state.manifest;
  const manifestHtml = m ? `
    <dl class="kv">
      <dt>Generated at</dt><dd>${esc(m.generated_at)}</dd>
      <dt>Git commit</dt><dd>${esc(m.git_commit || "not a git checkout")}</dd>
      <dt>Config hash</dt><dd>${esc(m.config_hash)}</dd>
      <dt>Judge model</dt><dd>${esc(m.judge && m.judge.model)}</dd>
      <dt>Judge backend used</dt><dd>${esc(m.judge && m.judge.backend_used)}</dd>
      <dt>Python</dt><dd>${esc(m.python_version)}</dd>
      ${Object.entries(m.input_hashes || {}).map(([k, v]) =>
        `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}
    </dl>
    <details><summary>full run_manifest.json</summary><pre>${esc(JSON.stringify(m, null, 2))}</pre></details>`
    : `<p class="hint">No run_manifest.json yet — run the pipeline first.</p>`;

  el.innerHTML = `
    <div class="card">
      <h2>LLM call log <span class="badge badge-neutral">${calls.length} attempt(s)</span></h2>
      <p class="hint">Every backend attempt is logged, including failed ones — a failed
      <code>live</code> attempt followed by a <code>stub</code> entry is the expected pattern
      when no API key is configured.</p>
      <div class="table-scroll">
        <table>
          <thead><tr><th>Backend</th><th>Used</th><th>Timestamp</th><th>Model</th><th>Prompt hash</th><th>Notes</th></tr></thead>
          <tbody>${callRows}</tbody>
        </table>
      </div>
      ${calls.length ? `<details><summary>raw llm_calls.jsonl</summary><pre>${
        esc(calls.map((c) => JSON.stringify(c)).join("\n"))}</pre></details>` : ""}
    </div>
    <div class="card"><h2>Run provenance</h2>${manifestHtml}</div>`;
}

/* ---------- tab: fixture ---------- */

function renderFixture() {
  const el = $("#tab-fixture");
  const c = state.input_counts;

  el.innerHTML = `
    <div class="card">
      <h2>Current inputs ${state.using_custom_fixture
        ? '<span class="badge badge-warn">custom</span>'
        : '<span class="badge badge-neutral">original sample</span>'}</h2>
      ${c ? `<dl class="kv">
        <dt>KB documents</dt><dd>${c.kb_docs}</dd>
        <dt>Queries</dt><dd>${c.queries}</dd>
        <dt>Variants</dt><dd>${c.variants} — ${esc((c.variant_names || []).join(", "))}</dd>
      </dl>` : '<p class="hint">Inputs are not currently loadable — see the error banner above.</p>'}
      ${state.kb ? `<details><summary>kb.json</summary><pre>${esc(JSON.stringify(state.kb, null, 2))}</pre></details>` : ""}
      ${state.queries ? `<details><summary>queries.json</summary><pre>${esc(JSON.stringify(state.queries, null, 2))}</pre></details>` : ""}
      ${state.answers ? `<details><summary>candidate_answers.json</summary><pre>${esc(JSON.stringify(state.answers, null, 2))}</pre></details>` : ""}
    </div>

    <div class="card">
      <h2>Swap in a different fixture</h2>
      <p class="hint">Upload a replacement set of all three input files. They are validated with
      the harness's own loader <em>before</em> anything is overwritten — if they fail schema or
      referential-integrity checks, the current fixture is left untouched and the errors are
      shown below. Then click <strong>Run Pipeline</strong> to evaluate the new data.</p>
      <form id="upload-form">
        <div class="upload-grid">
          <label for="f-kb">kb.json</label><input id="f-kb" type="file" accept=".json" required>
          <label for="f-queries">queries.json</label><input id="f-queries" type="file" accept=".json" required>
          <label for="f-answers">candidate_answers.json</label><input id="f-answers" type="file" accept=".json" required>
        </div>
        <button type="submit" class="btn btn-primary">Upload &amp; replace</button>
        <button type="button" id="btn-restore" class="btn btn-light">Restore original sample</button>
      </form>
      <div id="upload-result"></div>
    </div>`;

  $("#upload-form").addEventListener("submit", onUpload);
  $("#btn-restore").addEventListener("click", onRestore);
}

async function onUpload(event) {
  event.preventDefault();
  const out = $("#upload-result");
  const form = new FormData();
  form.append("kb", $("#f-kb").files[0]);
  form.append("queries", $("#f-queries").files[0]);
  form.append("answers", $("#f-answers").files[0]);

  out.innerHTML = '<p class="hint">Validating…</p>';
  const res = await fetch("/api/upload-fixture", { method: "POST", body: form });
  const data = await res.json();

  if (data.success) {
    out.innerHTML = '<div class="banner" style="background:var(--pass-bg);color:var(--pass);border-color:#b9e2cb">'
      + "Fixture replaced and validated. Click <strong>Run Pipeline</strong> to evaluate it.</div>";
    await refresh();
  } else {
    out.innerHTML = '<div class="banner"><strong>Upload rejected — the current fixture was not modified.</strong><ul>'
      + (data.errors || []).map((e) => `<li><code>${esc(e)}</code></li>`).join("") + "</ul></div>";
  }
}

async function onRestore() {
  await fetch("/api/restore-sample", { method: "POST" });
  await refresh();
  $("#upload-result").innerHTML =
    '<div class="banner" style="background:var(--pass-bg);color:var(--pass);border-color:#b9e2cb">'
    + "Original sample fixture restored.</div>";
}

/* ---------- wiring ---------- */

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.add("hidden"));
    $("#tab-" + tab.dataset.tab).classList.remove("hidden");
  });
});

$("#btn-run").addEventListener("click", () => runCommand("/api/run", "#btn-run", "Run Pipeline", "python run.py"));
$("#btn-validate").addEventListener("click", () => runCommand("/api/validate", "#btn-validate", "Validate", "python validate.py"));
$("#btn-test").addEventListener("click", () => runCommand("/api/test", "#btn-test", "Run Tests", "python -m pytest -q"));
$("#console-close").addEventListener("click", () => $("#console-panel").classList.add("hidden"));

refresh();

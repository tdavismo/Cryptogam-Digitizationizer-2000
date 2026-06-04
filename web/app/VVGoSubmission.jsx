/* global React, Region, Icon, ICONS, Pill, Placeholder */
const { useState: useStateS4, useRef: useRefS4, useEffect: useEffectS4, useMemo: useMemoS4 } = React;

/* ── Persistent VVGo screen state ─────────────────────────────────────────
   Mirrors window.__CDZ_SETUP_STATE / __CDZ_QC_STATE — keeps the user's
   selections (scope, advanced toggles, last submission table) across tab
   switches.  Persisted server-side config (token, model, prompt …) is
   loaded once on mount via GET /api/config. */
if (!window.__CDZ_VVGO_STATE) {
  window.__CDZ_VVGO_STATE = {
    advOpen:  false,
    scope:    "approved",   // "approved" | "all"
    rows:     {},           // crop_path → {status, ...}
    settings: null,         // hydrated from /api/config on first mount
  };
}
const VV = window.__CDZ_VVGO_STATE;

const VVGO_MODELS = [
  "gemini-3.1-flash-lite-preview",
  "gemini-3-flash-preview",
  "gemini-3.1-pro-preview",
];

const DEFAULT_SETTINGS = {
  vvgo_token:     "",
  vvgo_model:     VVGO_MODELS[0],
  vvgo_prompt:    "SLTPvM_default.yaml",
  vvgo_json_dir:  "",
  vvgo_workers:   4,
  vvgo_ocr:       "",       // "" = "Same as LLM"
  vvgo_wfo:       false,
  vvgo_cop90:     false,
  vvgo_ocr_only:  false,
};

/* Extract a clean prompt name from whatever a stored/returned value is.
   The VVGo /prompts items are dicts; older configs persisted the whole dict
   as a string ("{'filename': 'X.yaml', 'name': ...}"). Pull the filename or
   name out; if it doesn't look like a prompt name, fall back to the default. */
function _cleanPromptName(v) {
  if (typeof v !== "string" || !v) return DEFAULT_SETTINGS.vvgo_prompt;
  const t = v.trim();
  if (t.startsWith("{") || t.startsWith("[")) {
    const m = t.match(/'(?:filename|name|prompt_ref)'\s*:\s*'([^']+)'/);
    return m ? m[1] : DEFAULT_SETTINGS.vvgo_prompt;
  }
  return t;
}

/* ── Per-crop row in the submission table ───────────────────────────────── */
function SubRow({ r }) {
  const st = r.status || "queued";
  return (
    <div className={`subrow ${st}`}>
      <span className="mono crop-id" title={r.path}>{r.stem || r.name}</span>
      <Pill status={st} />
      <div className="trans-cell">
        {st === "complete" && r.json_path &&
          <div className="trans-preview">
            <span className="ts-sci">{r.sci || "extracted ✓"}</span>
            <span className="ts-meta" title={r.json_path}>{r.json_path.split(/[\\/]/).pop()}</span>
          </div>
        }
        {st === "submitted" && <span className="hint mono" style={{ color: "var(--blue)" }}>extracting text…</span>}
        {st === "queued"    && <span className="hint mono">— waiting —</span>}
        {st === "error"     && <span className="hint" style={{ color: "var(--red-bright)" }}>{r.error}</span>}
      </div>
      <div className="subrow-act">
        {st === "complete" && r.json_path &&
          <a className="btn btn-sm btn-ghost"
             href={"/api/file?path=" + encodeURIComponent(r.json_path).replace(/%2F/g,'/')}
             target="_blank" rel="noopener"
             title="Open JSON output">
            <Icon paths={ICONS.eye} size={13} /> View
          </a>
        }
        {st === "error" &&
          <button className="btn btn-sm" onClick={() => r.onRetry && r.onRetry(r)}>
            <Icon paths={ICONS.retry} size={13} /> Retry
          </button>
        }
      </div>
    </div>);
}

/* ── Empty state (no batch loaded yet) ─────────────────────────────────── */
function VVGoEmpty() {
  return (
    <div className="qc-empty">
      <div className="qc-empty-card">
        <Icon paths={ICONS.warn} size={28} />
        <div className="qc-empty-title">No batch loaded</div>
        <div className="qc-empty-sub">
          Run a segmentation on the <b>Session Setup</b> tab, then return here
          to submit the resulting crops to VoucherVision Go.
        </div>
      </div>
    </div>);
}

function VVGoSubmission() {
  const session   = window.__CDZ_SESSION  || null;
  const qcState   = window.__CDZ_QC_STATE || { overrides: {} };

  /* Settings — initial values from server config on mount.  Form fields are
     controlled and persisted back via PUT /api/config on Save. */
  const [s, setS] = useStateS4(() => VV.settings || DEFAULT_SETTINGS);

  /* Crops available for submission — pulled from the output dir of the last
     batch (window.__CDZ_SESSION.outputDir). */
  const [crops,    setCrops]    = useStateS4([]);
  const [loadErr,  setLoadErr]  = useStateS4(null);

  /* UI state */
  const [scope,        setScope]      = useStateS4(VV.scope);   // "approved" | "all"
  const [advOpen,      setAdvOpen]    = useStateS4(VV.advOpen);
  const [showToken,    setShowToken]  = useStateS4(false);
  const [savedFlash,   setSavedFlash] = useStateS4(false);
  const [knownPrompts, setKnownPrompts] = useStateS4([s.vvgo_prompt || DEFAULT_SETTINGS.vvgo_prompt]);
  const [fetchingPr,   setFetchingPr] = useStateS4(false);

  /* Submission state */
  const [running, setRunning] = useStateS4(false);
  const [rows,    setRows]    = useStateS4(VV.rows);   // path → {status, ...}
  const abortRef = useRefS4(null);

  /* Mirror persisted bits into the global bag whenever they change. */
  useEffectS4(() => { VV.scope    = scope; },    [scope]);
  useEffectS4(() => { VV.advOpen  = advOpen; },  [advOpen]);
  useEffectS4(() => { VV.settings = s; },        [s]);
  useEffectS4(() => { VV.rows     = rows; },     [rows]);

  /* Hydrate persisted config from the server on first ever mount.  We use
     an explicit VV.hydrated flag rather than checking VV.settings, because
     the mirror effect above writes VV.settings = DEFAULT on first render
     (effects run in declaration order) which would otherwise short-circuit
     the load. */
  useEffectS4(() => {
    if (VV.hydrated) return;
    VV.hydrated = true;
    fetch("/api/config")
      .then((r) => r.ok ? r.json() : {})
      .then((cfg) => {
        const merged = { ...DEFAULT_SETTINGS, ...cfg };
        if (!merged.vvgo_json_dir && session && session.outputDir) {
          /* Default JSON output = <outputDir>/vvgo_json on first run */
          const sep = session.outputDir.includes("\\") ? "\\" : "/";
          merged.vvgo_json_dir = session.outputDir.replace(/[\\/]+$/, "") + sep + "vvgo_json";
        }
        /* Heal a prompt value persisted before the name-extraction fix: an old
           config may hold a stringified dict ("{'author': ...}"). Pull the
           filename/name out of it, or fall back to the default. */
        merged.vvgo_prompt = _cleanPromptName(merged.vvgo_prompt);
        if (merged.vvgo_prompt && !knownPrompts.includes(merged.vvgo_prompt)) {
          setKnownPrompts([merged.vvgo_prompt, ...knownPrompts]);
        }
        setS(merged);
      })
      .catch(() => setS(DEFAULT_SETTINGS));
  }, []);

  /* Load the crop list whenever the active batch changes. */
  useEffectS4(() => {
    if (!session || !session.outputDir) return;
    setLoadErr(null);
    fetch("/api/crops?output_dir=" + encodeURIComponent(session.outputDir))
      .then((r) => r.ok ? r.json() : r.json().then((j) => Promise.reject(j.detail || r.statusText)))
      .then((j) => setCrops((j.crops || []).map((c) => ({
        path: c.path, name: c.name, stem: c.stem,
      }))))
      .catch((e) => setLoadErr(String(e)));
  }, [session && session.outputDir]);

  /* Scope → which crop paths are eligible for submission. */
  const cropsScoped = useMemoS4(() => {
    if (scope === "all") return crops;
    const ov = qcState.overrides || {};
    return crops.filter((c) => ov[c.path] === "approved");
  }, [crops, scope]);

  /* Counters derived from the live rows map. */
  const counts = useMemoS4(() => {
    const c = { complete: 0, submitted: 0, error: 0, queued: 0 };
    for (const k in rows) c[rows[k].status] = (c[rows[k].status] || 0) + 1;
    return c;
  }, [rows]);

  /* Update one field in `s` and (debounced via direct PUT) persist server-side */
  function patchSetting(key, value) {
    setS((prev) => ({ ...prev, [key]: value }));
  }
  function persistSettings(extra = {}) {
    const payload = { ...s, ...extra };
    return fetch("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  function saveSettings() {
    persistSettings()
      .then((r) => r.ok ? r.json() : Promise.reject())
      .then(() => { setSavedFlash(true); setTimeout(() => setSavedFlash(false), 1400); })
      .catch(() => {});
  }

  async function fetchPrompts() {
    if (!s.vvgo_token.trim()) { alert("Enter your API token first."); return; }
    setFetchingPr(true);
    try {
      const r = await fetch("/api/vvgo-prompts?token=" + encodeURIComponent(s.vvgo_token));
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.detail || ("HTTP " + r.status));
      }
      const j = await r.json();
      setKnownPrompts(j.prompts);
      if (!j.prompts.includes(s.vvgo_prompt)) patchSetting("vvgo_prompt", j.prompts[0]);
    } catch (e) {
      alert("Could not load prompts: " + e.message);
    } finally {
      setFetchingPr(false);
    }
  }

  /* ── Submission ──────────────────────────────────────────────────────── */
  async function startSubmission() {
    if (running) {                      // acts as Cancel while a run streams
      if (abortRef.current) abortRef.current.abort();
      return;
    }
    if (!s.vvgo_token.trim()) {
      alert("Enter your API token first (click Save to persist it).");
      return;
    }
    if (cropsScoped.length === 0) {
      alert(scope === "approved"
        ? "No approved crops to submit. Approve some in QC Review, or switch scope to \"All crops\"."
        : "No crops to submit.");
      return;
    }
    if (!s.vvgo_json_dir) {
      alert("Set a JSON output folder first.");
      return;
    }

    /* Seed every selected crop as "queued" so the table populates immediately. */
    const seed = {};
    for (const c of cropsScoped) seed[c.path] = { path: c.path, name: c.name, stem: c.stem, status: "queued" };
    setRows(seed);

    setRunning(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    try {
      const body = {
        token: s.vvgo_token,
        model: s.vvgo_model,
        prompt: s.vvgo_prompt,
        json_dir: s.vvgo_json_dir,
        crop_paths: cropsScoped.map((c) => c.path),
        max_workers: s.vvgo_workers,
        ocr_engine: s.vvgo_ocr,
        include_wfo: s.vvgo_wfo,
        include_cop90: s.vvgo_cop90,
        ocr_only: s.vvgo_ocr_only,
      };
      const res = await fetch("/api/vvgo-submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch (e) {}
        throw new Error(detail);
      }
      /* Parse SSE manually off the fetch reader */
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const chunk = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const line = chunk.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          const evt = JSON.parse(line.slice(5).trim());
          if (evt.type === "progress") {
            setRows((prev) => ({
              ...prev,
              [evt.path]: {
                ...(prev[evt.path] || {}),
                path: evt.path, name: evt.name,
                stem: (prev[evt.path] && prev[evt.path].stem) || evt.name.replace(/\.[^.]+$/, ""),
                status: evt.ok ? "complete" : "error",
                json_path: evt.json_path || null,
                error: evt.error || null,
              },
            }));
          }
        }
      }
    } catch (err) {
      if (err.name !== "AbortError") alert("Submission failed: " + err.message);
    } finally {
      setRunning(false);
      abortRef.current = null;
    }
  }

  /* ── Empty state ─────────────────────────────────────────────────────── */
  if (!session) return <VVGoEmpty />;

  /* Visible rows: union of the seeded/queued set and any inflight updates */
  const visibleRows = cropsScoped.map((c) => rows[c.path] ||
    { path: c.path, name: c.name, stem: c.stem, status: "queued" });

  const approvedCount = Object.values(qcState.overrides || {}).filter((v) => v === "approved").length;
  const eligibleTotal = cropsScoped.length;
  const pctComplete   = eligibleTotal ? (counts.complete   / eligibleTotal) * 100 : 0;
  const pctSubmitting = eligibleTotal ? (counts.submitted  / eligibleTotal) * 100 : 0;
  const pctError      = eligibleTotal ? (counts.error      / eligibleTotal) * 100 : 0;

  return (
    <div className="submit">

      {/* Header / controls — token, model, prompt, scope, Start.
          Single-row toolbar (no wrap) matching the QC toolbar. */}
      <div className="submit-top">

        <div className="st-field st-token">
          <label className="label">API token</label>
          <div className="input-row">
            <input className="input" type={showToken ? "text" : "password"}
              value={s.vvgo_token}
              placeholder="vvgo_sk_…"
              onChange={(e) => patchSetting("vvgo_token", e.target.value)} />
            <button className="btn btn-icon" title={showToken ? "Hide" : "Show"}
              onClick={() => setShowToken((v) => !v)}>
              <Icon paths={ICONS.eye} />
            </button>
            <button className="btn btn-icon" title="Save (persist to config)"
              onClick={saveSettings} style={savedFlash ? { color: "var(--green)" } : null}>
              <Icon d={savedFlash ? ICONS.check : ICONS.download} />
            </button>
          </div>
        </div>

        <div className="st-field st-model">
          <label className="label">Model</label>
          <div className="select-wrap">
            <select className="input sans" value={s.vvgo_model}
              onChange={(e) => patchSetting("vvgo_model", e.target.value)}>
              {VVGO_MODELS.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </div>
        </div>

        <div className="st-field st-prompt">
          <label className="label">Prompt</label>
          <div className="input-row">
            <div className="select-wrap" style={{ flex: 1, minWidth: 0 }}>
              <select className="input sans" value={s.vvgo_prompt}
                title={s.vvgo_prompt}
                onChange={(e) => patchSetting("vvgo_prompt", e.target.value)}>
                {knownPrompts.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
            </div>
            <button className="btn btn-sm" onClick={fetchPrompts}
              disabled={fetchingPr || !s.vvgo_token.trim()}
              title="Load the prompt list from the VVGo server">
              {fetchingPr ? "…" : "Fetch"}
            </button>
          </div>
        </div>

        <div className="st-field" style={{ flexShrink: 0 }}>
          <label className="label">Scope</label>
          <div className="filterbar" style={{ flexWrap: "nowrap" }}>
            <button className={`fchip ${scope === "approved" ? "on" : ""}`}
              onClick={() => setScope("approved")}>
              Approved<span className="fn">{approvedCount}</span>
            </button>
            <button className={`fchip ${scope === "all" ? "on" : ""}`}
              onClick={() => setScope("all")}>
              All<span className="fn">{crops.length}</span>
            </button>
          </div>
        </div>

        <div className="st-actions">
          <button className="btn btn-sm" onClick={() => setAdvOpen((v) => !v)}>
            <Icon d={ICONS.filter} size={13} /> Advanced {advOpen ? "▴" : "▾"}
          </button>
          <button className="btn btn-primary"
            onClick={startSubmission}
            disabled={!eligibleTotal && !running}>
            <Icon d={running ? ICONS.x : ICONS.send} size={15} />
            {running ? "Cancel" : `Submit (${eligibleTotal})`}
          </button>
        </div>
      </div>

      {/* Advanced settings — mirrors the desktop dialog */}
      {advOpen &&
        <div className="adv-panel adv-panel-vvgo">
          <span className="adv-panel-title">Advanced VVGo Settings</span>

          <div className="adv-field">
            <label className="label">JSON output folder</label>
            <div className="input-row">
              <input className="input" value={s.vvgo_json_dir}
                placeholder="(output)/vvgo_json"
                onChange={(e) => patchSetting("vvgo_json_dir", e.target.value)} />
              <button className="btn btn-icon"
                onClick={async () => {
                  try {
                    const r = await fetch("/api/pick-folder?title=" +
                      encodeURIComponent("Choose JSON output folder") +
                      (s.vvgo_json_dir ? "&initial=" + encodeURIComponent(s.vvgo_json_dir) : ""));
                    const j = await r.json();
                    if (j.path) patchSetting("vvgo_json_dir", j.path);
                  } catch (e) {}
                }}>
                <Icon d={ICONS.folder} />
              </button>
            </div>
          </div>

          <div className="adv-field">
            <label className="label">OCR engine</label>
            <div className="select-wrap">
              <select className="input sans" value={s.vvgo_ocr}
                onChange={(e) => patchSetting("vvgo_ocr", e.target.value)}>
                <option value="">Same as LLM (recommended)</option>
                {VVGO_MODELS.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </div>
          </div>

          <div className="adv-field" style={{ gridColumn: "1 / -1" }}>
            <label className="label">Parallel workers — {s.vvgo_workers}</label>
            <input type="range" className="slider" min="1" max="16" step="1"
              value={s.vvgo_workers}
              onChange={(e) => patchSetting("vvgo_workers", +e.target.value)} />
          </div>

          <label className="check-row" style={{ gridColumn: "1 / -1" }}>
            <input type="checkbox" checked={s.vvgo_wfo}
              onChange={(e) => patchSetting("vvgo_wfo", e.target.checked)} />
            <span>World Flora Online taxonomic validation
              <span className="hint" style={{ marginLeft: 6 }}>— slower; checks scientific names against WFO</span></span>
          </label>

          <label className="check-row" style={{ gridColumn: "1 / -1" }}>
            <input type="checkbox" checked={s.vvgo_cop90}
              onChange={(e) => patchSetting("vvgo_cop90", e.target.checked)} />
            <span>Add COP90 elevation data from coordinates</span>
          </label>

          <label className="check-row" style={{ gridColumn: "1 / -1" }}>
            <input type="checkbox" checked={s.vvgo_ocr_only}
              onChange={(e) => patchSetting("vvgo_ocr_only", e.target.checked)} />
            <span>OCR only (skip JSON parsing)</span>
          </label>
        </div>
      }

      {/* Progress card */}
      <div className="progress-card panel">
        <div className="panel-body">
          <div className="row between" style={{ marginBottom: 10 }}>
            <span className="mono" style={{ fontSize: 12.5, color: "var(--text-2)" }}>
              {running
                ? `Submitting — ${counts.complete + counts.error} / ${eligibleTotal}`
                : eligibleTotal === 0
                  ? "Nothing to submit"
                  : `${counts.complete} / ${eligibleTotal} complete`}
            </span>
            <div className="row" style={{ gap: 14, flexWrap: "wrap" }}>
              <span className="pill green"><span className="pdot" />{counts.complete  || 0} complete</span>
              <span className={`pill blue${running ? " pulse" : ""}`}><span className="pdot" />{counts.submitted || 0} submitting</span>
              <span className="pill red"><span className="pdot" />{counts.error      || 0} error</span>
              <span className="pill neutral"><span className="pdot" />{Math.max(0, eligibleTotal - counts.complete - counts.error - counts.submitted)} queued</span>
            </div>
          </div>
          <div className="progress-track">
            <div className="progress-seg green" style={{ width: pctComplete + "%" }} />
            <div className="progress-seg blue"  style={{ width: pctSubmitting + "%" }} />
            <div className="progress-seg red"   style={{ width: pctError + "%" }} />
          </div>
          <div className="row between" style={{ marginTop: 8 }}>
            <span className="hint">JSON results: <span className="mono">{s.vvgo_json_dir || "(set a folder above)"}</span></span>
            <span className="hint">{loadErr ? <span style={{ color: "var(--red-bright)" }}>⚠ {loadErr}</span> : <span className="mono">{crops.length} crops in batch</span>}</span>
          </div>
        </div>
      </div>

      {/* Per-crop table */}
      <div className="subtable grow">
        <div className="panel" style={{ height: "100%", display: "flex", flexDirection: "column" }}>
          <div className="subtable-head">
            <span>Crop</span><span>Status</span>
            <span>Output</span>
            <span></span>
          </div>
          <div className="subtable-body">
            {visibleRows.length === 0 &&
              <div className="qc-msg">
                {scope === "approved"
                  ? "No approved crops yet. Approve crops in QC Review, or switch scope to All."
                  : "No crops in the current batch."}
              </div>
            }
            {visibleRows.map((r) => <SubRow key={r.path} r={r} />)}
          </div>
        </div>
      </div>
    </div>);
}

Object.assign(window, { VVGoSubmission });

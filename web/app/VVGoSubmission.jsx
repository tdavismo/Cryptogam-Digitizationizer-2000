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

/* Pin the JSON output folder to the *current* batch.

   vvgo_json_dir is a persisted global setting, so a value auto-defaulted for
   one folder ("<FolderA>/vvgo_json") would otherwise linger after you open a
   different processed folder — making the audit read receipts from the wrong
   place and report far too few "received". We retarget only the auto-shaped
   "<folder>/vvgo_json" value (or an empty one); a path the user deliberately
   pointed somewhere else is left untouched. */
function _retargetJsonDir(outputDir, cur) {
  if (!outputDir) return cur || "";
  const base = outputDir.replace(/[\\/]+$/, "");
  const sep  = base.includes("\\") ? "\\" : "/";
  const want = base + sep + "vvgo_json";
  const c = (cur || "").replace(/[\\/]+$/, "");
  const autoShaped = /[\\/]vvgo_json$/i.test(c);
  if (!c || (autoShaped && c.toLowerCase() !== want.toLowerCase())) return want;
  return cur;
}

/* ── Per-crop row in the submission table ───────────────────────────────── */
function SubRow({ r, onView, onHistory, onSubmitOne, jsonDir }) {
  const st = r.status || "queued";
  const a  = r.audit;
  const attempts = (a && a.attempts) || [];
  /* JSON path on disk = jsonDir + the audit's json_name. Prefer the live
     run's json_path when present (just written this session). */
  const sep = (jsonDir || "").includes("\\") ? "\\" : "/";
  const jsonPath = r.json_path ||
    (a && a.received && jsonDir ? jsonDir.replace(/[\\/]+$/, "") + sep + a.json_name : null);
  const lastAttempt = attempts.length ? attempts[attempts.length - 1] : null;

  return (
    <div className={`subrow ${st}`}>
      <span className="mono crop-id" title={r.path}>{r.stem || r.name}</span>
      <Pill status={st} />
      <div className="trans-cell">
        {(st === "received" || st === "complete") &&
          <div className="trans-preview">
            <span className="ts-sci">extracted ✓</span>
            <span className="ts-meta" title={jsonPath || ""}>
              {(jsonPath && jsonPath.split(/[\\/]/).pop()) || (a && a.json_name) || "output written"}
            </span>
          </div>
        }
        {st === "submitted" && <span className="hint mono" style={{ color: "var(--blue)" }}>extracting text…</span>}
        {st === "queued"    && <span className="hint mono">— ready to submit —</span>}
        {(st === "errored" || st === "error") &&
          <span className="hint" style={{ color: "var(--red-bright)" }}>{r.error || (lastAttempt && lastAttempt.error) || "error"}</span>}
        {st === "needs_output" &&
          <span className="hint mono" style={{ color: "var(--gold)" }}>submitted, no JSON yet — resubmit</span>}
        {st === "not_submitted" && <span className="hint mono">— not submitted —</span>}
      </div>
      <div className="subrow-act" style={{ display: "flex", gap: 6 }}>
        {jsonPath && (st === "received" || st === "complete") &&
          <button className="btn btn-sm btn-ghost"
             onClick={() => onView(jsonPath)} title="Preview JSON output">
            <Icon paths={ICONS.eye} size={13} /> View
          </button>
        }
        {attempts.length > 0 &&
          <button className="btn btn-sm btn-ghost"
             onClick={() => onHistory(r)} title={`${attempts.length} submission attempt(s)`}>
            <Icon paths={ICONS.doc || ICONS.eye} size={13} /> {attempts.length}
          </button>
        }
        {(st === "needs_output" || st === "errored" || st === "not_submitted") && onSubmitOne &&
          <button className="btn btn-sm" onClick={() => onSubmitOne(r)} title="Submit this crop now">
            <Icon paths={ICONS.retry} size={13} /> {st === "not_submitted" ? "Submit" : "Resubmit"}
          </button>
        }
      </div>
    </div>);
}

/* ── Submission attempt-history modal — the 'diet VVGo Editor' audit view ─── */
function AuditModal({ row, jsonDir, onClose, onView }) {
  const a = row.audit || {};
  const attempts = a.attempts || [];
  const sep = (jsonDir || "").includes("\\") ? "\\" : "/";
  const jsonPath = a.received && jsonDir
    ? jsonDir.replace(/[\\/]+$/, "") + sep + a.json_name : null;
  const fmt = (ts) => { try { return new Date(ts).toLocaleString(); } catch (e) { return ts; } };

  return (
    <div className="json-modal-backdrop" onMouseDown={onClose}>
      <div className="json-modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="json-modal-head">
          <span className="panel-title">Audit · {row.stem || row.name}</span>
          <button className="sd-close" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="json-modal-body">
          <div className="meta-grid json-fields" style={{ marginBottom: 12 }}>
            <span className="mk">Segmented</span>
            <span className="mv" style={{ textAlign: "left" }}>{a.segmented_at ? fmt(a.segmented_at) : "—"}</span>
            <span className="mk">Status</span>
            <span className="mv" style={{ textAlign: "left" }}><Pill status={a.status || row.status} /></span>
            <span className="mk">Output JSON</span>
            <span className="mv" style={{ textAlign: "left" }}>
              {a.received
                ? <button className="btn btn-sm btn-ghost" onClick={() => onView(jsonPath)}>
                    <Icon paths={ICONS.eye} size={13} /> {a.json_name}
                  </button>
                : <span className="hint">none on disk</span>}
            </span>
          </div>
          <div className="sd-section-label" style={{ marginBottom: 6 }}>
            Submission attempts ({attempts.length})
          </div>
          {attempts.length === 0 && <div className="hint">No submissions recorded.</div>}
          {attempts.map((at, i) =>
            <div key={i} className={`audit-attempt ${at.ok ? "ok" : "err"}`}>
              <div className="row between">
                <span className="mono" style={{ fontSize: 11 }}>{fmt(at.ts)}</span>
                <span className={at.ok ? "pill green" : "pill red"} style={{ fontSize: 10 }}>
                  <span className="pdot" />{at.ok ? "ok" : "error"}
                </span>
              </div>
              <div className="hint mono" style={{ marginTop: 3 }}>
                {at.model} · {at.prompt}
              </div>
              {!at.ok && at.error &&
                <div className="hint" style={{ color: "var(--red-bright)", marginTop: 3 }}>{at.error}</div>}
            </div>
          )}
        </div>
      </div>
    </div>);
}

/* ── JSON preview modal ──────────────────────────────────────────────────
   Fetches /api/json and renders the record: a friendly key/value table for
   the parsed fields plus the raw JSON in a scrollable pre. Replaces the old
   "open in a new tab" behaviour that made the browser try to render JSON as
   an image. */
function JsonModal({ path, onClose }) {
  const [state, setState] = useStateS4({ loading: true, data: null, err: null, name: "" });

  useEffectS4(() => {
    let alive = true;
    fetch("/api/json?path=" + encodeURIComponent(path))
      .then((r) => r.ok ? r.json() : r.json().then((j) => Promise.reject(j.detail || r.statusText)))
      .then((j) => alive && setState({ loading: false, data: j.data, err: null, name: j.name }))
      .catch((e) => alive && setState({ loading: false, data: null, err: String(e), name: "" }));
    return () => { alive = false; };
  }, [path]);

  /* Flatten the most useful transcription fields. VVGo records usually nest the
     parsed values under formatted_json / ocr; surface those when present. */
  const record = state.data && (
    state.data.formatted_json || state.data.ocr || state.data
  );
  const rows = record && typeof record === "object" && !Array.isArray(record)
    ? Object.entries(record).filter(([, v]) => v !== null && v !== "" && typeof v !== "object")
    : [];

  return (
    <div className="json-modal-backdrop" onMouseDown={onClose}>
      <div className="json-modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="json-modal-head">
          <span className="panel-title">{state.name || "JSON record"}</span>
          <button className="sd-close" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="json-modal-body">
          {state.loading && <div className="hint">Loading…</div>}
          {state.err && <div className="qc-msg err">⚠ {state.err}</div>}
          {!state.loading && !state.err && <>
            {rows.length > 0 &&
              <div className="meta-grid json-fields">
                {rows.map(([k, v]) => <React.Fragment key={k}>
                  <span className="mk">{k}</span>
                  <span className="mv" style={{ textAlign: "left" }}>{String(v)}</span>
                </React.Fragment>)}
              </div>
            }
            <details open={rows.length === 0}>
              <summary className="hint" style={{ cursor: "pointer", marginTop: 8 }}>Raw JSON</summary>
              <pre className="json-raw">{JSON.stringify(state.data, null, 2)}</pre>
            </details>
          </>}
        </div>
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
  const [skipSubmitted, setSkipSubmitted] = useStateS4(VV.skipSubmitted !== false); // default on
  const [advOpen,      setAdvOpen]    = useStateS4(VV.advOpen);
  const [showToken,    setShowToken]  = useStateS4(false);
  const [savedFlash,   setSavedFlash] = useStateS4(false);
  const [knownPrompts, setKnownPrompts] = useStateS4([s.vvgo_prompt || DEFAULT_SETTINGS.vvgo_prompt]);
  const [fetchingPr,   setFetchingPr] = useStateS4(false);

  /* Submission state */
  const [running, setRunning] = useStateS4(false);
  const [rows,    setRows]    = useStateS4(VV.rows);   // path → {status, ...}
  const [viewJson, setViewJson] = useStateS4(null);    // json_path being previewed
  const abortRef = useRefS4(null);

  /* Audit — the persistent, authoritative record. `auditByPath` maps crop path
     → audit entry (status received|needs_output|errored|not_submitted, attempts
     history). `auditTotals` drives the batch-wide progress so filter buttons
     no longer distort it. Loaded on mount and re-loaded after each submission. */
  const [auditByPath, setAuditByPath] = useStateS4({});
  const [auditTotals, setAuditTotals] = useStateS4(null);
  const [viewAudit,   setViewAudit]   = useStateS4(null);  // crop path → history modal

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
        /* Default/retarget JSON output to <outputDir>/vvgo_json for the folder
           we're actually looking at (a persisted value may belong to a prior
           folder). */
        merged.vvgo_json_dir = _retargetJsonDir(session && session.outputDir, merged.vvgo_json_dir);
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

  /* Whenever the active batch changes, pin the JSON output folder to it so the
     audit reads receipts from *this* folder's vvgo_json (a persisted path from a
     prior folder would otherwise undercount "received"). */
  useEffectS4(() => {
    if (!session || !session.outputDir) return;
    setS((prev) => {
      const next = _retargetJsonDir(session.outputDir, prev.vvgo_json_dir);
      return next === prev.vvgo_json_dir ? prev : { ...prev, vvgo_json_dir: next };
    });
  }, [session && session.outputDir]);

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

  /* Pull the authoritative audit (per-crop received/needs_output/errored +
     attempt history) from the server. JSON presence on disk is the source of
     truth for 'received', so this is robust across reloads and reopened
     folders. */
  function loadAudit() {
    if (!session || !session.outputDir) return Promise.resolve();
    const jd = (s && s.vvgo_json_dir) || "";
    const url = "/api/audit?output_dir=" + encodeURIComponent(session.outputDir) +
                (jd ? "&json_dir=" + encodeURIComponent(jd) : "");
    return fetch(url)
      .then((r) => r.ok ? r.json() : Promise.reject(r.statusText))
      .then((j) => {
        const byPath = {};
        for (const c of j.crops || []) byPath[c.path] = c;
        setAuditByPath(byPath);
        setAuditTotals(j.totals || null);
      })
      .catch(() => { /* non-fatal — UI falls back to live rows */ });
  }
  /* Re-audit when the batch or the JSON output folder changes. */
  useEffectS4(() => { loadAudit(); },
    [session && session.outputDir, s && s.vvgo_json_dir]);

  /* Scope → which crop paths are eligible for submission.
       base:    "approved" (QC-approved only) | "all"
       needsOnly: drop crops that already have a JSON output (audit 'received')
                  so a re-run only fills the gaps — the duplicate-avoidance the
                  user actually wants, driven by the authoritative audit. */
  const cropsScoped = useMemoS4(() => {
    let list = scope === "all"
      ? crops
      : crops.filter((c) => (qcState.overrides || {})[c.path] === "approved");
    if (skipSubmitted) {
      list = list.filter((c) => {
        const a = auditByPath[c.path];
        return !(a && a.received);     // keep only crops still missing output
      });
    }
    return list;
  }, [crops, scope, skipSubmitted, auditByPath]);

  /* How many of the currently-scoped crops still need an output, for the
     Needs-output chip count. */
  const needsOutputCount = useMemoS4(() => {
    const base = scope === "all"
      ? crops
      : crops.filter((c) => (qcState.overrides || {})[c.path] === "approved");
    return base.filter((c) => { const a = auditByPath[c.path]; return !(a && a.received); }).length;
  }, [crops, scope, auditByPath]);

  /* Live in-run counters from the rows map (used only while a submission is
     streaming, for the moment-to-moment view). */
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

  /* ── Submission ──────────────────────────────────────────────────────────
     `targets` defaults to the scoped list; a single-crop resubmit passes [crop].
     Same streaming pipeline either way. */
  async function startSubmission(targets) {
    if (running) {                      // acts as Cancel while a run streams
      if (abortRef.current) abortRef.current.abort();
      return;
    }
    if (!s.vvgo_token.trim()) {
      alert("Enter your API token first (click Save to persist it).");
      return;
    }
    const list = targets && targets.length ? targets : cropsScoped;
    if (list.length === 0) {
      alert(scope === "approved"
        ? "No approved crops to submit. Approve some in QC Review, or switch scope to \"All crops\"."
        : "No crops to submit.");
      return;
    }
    if (!s.vvgo_json_dir) {
      alert("Set a JSON output folder first.");
      return;
    }

    /* Seed each selected crop as "queued" so the table populates immediately.
       Merge into existing rows so a single resubmit doesn't wipe the table. */
    setRows((prev) => {
      const next = { ...prev };
      for (const c of list) next[c.path] = { path: c.path, name: c.name, stem: c.stem, status: "queued" };
      return next;
    });

    setRunning(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    try {
      const body = {
        token: s.vvgo_token,
        model: s.vvgo_model,
        prompt: s.vvgo_prompt,
        json_dir: s.vvgo_json_dir,
        crop_paths: list.map((c) => c.path),
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
      /* The batch-wide progress bar reads auditTotals (json-on-disk truth).
         Re-pull it during the run — throttled — so the bar advances as outputs
         land, instead of only on completion or a tab switch. */
      let lastAuditTick = 0;
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
            /* Record a successful submission so QC can badge it and the user
               can skip re-submitting. Shared QC state, keyed by crop path. */
            if (evt.ok && evt.path && window.__CDZ_QC_STATE) {
              if (!window.__CDZ_QC_STATE.submitted) window.__CDZ_QC_STATE.submitted = {};
              window.__CDZ_QC_STATE.submitted[evt.path] = true;
            }
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
            /* Advance the batch-wide progress live (throttled ≥900 ms so a fast
               run doesn't hammer the audit endpoint). */
            const now = performance.now();
            if (now - lastAuditTick > 900) { lastAuditTick = now; loadAudit(); }
          }
        }
      }
    } catch (err) {
      if (err.name !== "AbortError") alert("Submission failed: " + err.message);
    } finally {
      setRunning(false);
      abortRef.current = null;
      /* Re-pull the authoritative audit so receipts/badges reflect the JSON
         files just written (and any errors recorded). */
      loadAudit();
    }
  }

  /* ── Empty state ─────────────────────────────────────────────────────── */
  if (!session) return <VVGoEmpty />;

  /* Visible rows: each scoped crop, merged with (1) any live in-run status and
     (2) the persistent audit status. Live status wins while a submission is
     streaming; otherwise the audit (json-on-disk) is the source of truth. */
  const visibleRows = cropsScoped.map((c) => {
    const live = rows[c.path];
    const a = auditByPath[c.path];
    if (live && (live.status === "complete" || live.status === "error" || running)) {
      return { ...c, ...live, audit: a };
    }
    return {
      path: c.path, name: c.name, stem: c.stem,
      status: a ? a.status : "queued",
      json_path: a && a.received ? null : null,   // View uses audit json_name
      error: a ? a.last_error : null,
      audit: a,
    };
  });

  const approvedCount = Object.values(qcState.overrides || {}).filter((v) => v === "approved").length;
  const eligibleTotal = cropsScoped.length;

  /* Batch-wide progress reads from the audit totals (whole folder), NOT the
     filtered rows — so toggling scope/needs-output no longer warps the bar. */
  const bt = auditTotals || { total: crops.length, received: 0, needs_output: 0, errored: 0, not_submitted: crops.length };
  const batchTotal   = bt.total || crops.length || 0;
  const pctReceived  = batchTotal ? (bt.received / batchTotal) * 100 : 0;
  const pctErrored   = batchTotal ? (bt.errored  / batchTotal) * 100 : 0;
  const pctNeeds     = batchTotal ? ((bt.needs_output || 0) / batchTotal) * 100 : 0;

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
            <button className={`fchip ${skipSubmitted ? "on" : ""}`}
              title="Only submit crops that don't yet have a JSON output (skip already-received)"
              onClick={() => { const v = !skipSubmitted; VV.skipSubmitted = v; setSkipSubmitted(v); }}>
              Needs output<span className="fn">{needsOutputCount}</span>
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

      {/* Progress card — reads the batch-wide audit totals, so the readout
          reflects the whole folder's receipt state and never shifts when the
          user toggles a scope/needs-output filter. While a submission streams,
          the headline shows live progress; the pills/bar stay batch-wide. */}
      <div className="progress-card panel">
        <div className="panel-body">
          <div className="row between" style={{ marginBottom: 10 }}>
            <span className="mono" style={{ fontSize: 12.5, color: "var(--text-2)" }}>
              {running
                ? `Submitting — ${counts.complete + counts.error} / ${eligibleTotal} this run`
                : `${bt.received} / ${batchTotal} received`}
            </span>
            <div className="row" style={{ gap: 14, flexWrap: "wrap" }}>
              <span className="pill green"><span className="pdot" />{bt.received || 0} received</span>
              <span className="pill gold"><span className="pdot" />{bt.needs_output || 0} needs output</span>
              <span className="pill red"><span className="pdot" />{bt.errored || 0} errored</span>
              <span className="pill neutral"><span className="pdot" />{bt.not_submitted || 0} not submitted</span>
            </div>
          </div>
          <div className="progress-track" title={`${bt.received}/${batchTotal} crops have a JSON output`}>
            <div className="progress-seg green" style={{ width: pctReceived + "%" }} />
            <div className="progress-seg gold"  style={{ width: pctNeeds + "%" }} />
            <div className="progress-seg red"   style={{ width: pctErrored + "%" }} />
          </div>
          <div className="row between" style={{ marginTop: 8 }}>
            <span className="hint">JSON results: <span className="mono">{s.vvgo_json_dir || "(set a folder above)"}</span></span>
            <span className="hint">{loadErr ? <span style={{ color: "var(--red-bright)" }}>⚠ {loadErr}</span> : <span className="mono">{crops.length} crops · {bt.received} with output</span>}</span>
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
            {visibleRows.map((r) => <SubRow key={r.path} r={r}
              jsonDir={s.vvgo_json_dir}
              onView={setViewJson}
              onHistory={(row) => setViewAudit(row)}
              onSubmitOne={(row) => startSubmission([{ path: row.path, name: row.name, stem: row.stem }])}
            />)}
          </div>
        </div>
      </div>

      {viewJson && <JsonModal path={viewJson} onClose={() => setViewJson(null)} />}
      {viewAudit && <AuditModal row={viewAudit} jsonDir={s.vvgo_json_dir}
        onClose={() => setViewAudit(null)}
        onView={(p) => { setViewAudit(null); setViewJson(p); }} />}
    </div>);
}

Object.assign(window, { VVGoSubmission });

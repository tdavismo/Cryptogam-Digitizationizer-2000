/* global React, Region, Icon, ICONS, Pill, Placeholder */

const SUB_ROWS = [
{ id: "IMG_0241_01", st: "complete", sci: "Cladonia rangiferina", coll: "M. Oldham", date: "1998-08-14" },
{ id: "IMG_0241_02", st: "complete", sci: "Peltigera aphthosa", coll: "M. Oldham", date: "1998-08-14" },
{ id: "IMG_0241_03", st: "complete", sci: "Hypogymnia physodes", coll: "J. Macoun", date: "1901-07-02" },
{ id: "IMG_0241_04", st: "submitted", sci: null, coll: null, date: null },
{ id: "IMG_0241_05", st: "submitted", sci: null, coll: null, date: null },
{ id: "IMG_0242_01", st: "error", sci: null, coll: null, date: null, err: "Timeout after 60s — server busy" },
{ id: "IMG_0242_02", st: "queued", sci: null, coll: null, date: null },
{ id: "IMG_0242_03", st: "queued", sci: null, coll: null, date: null },
{ id: "IMG_0243_01", st: "queued", sci: null, coll: null, date: null }];


function SubRow({ r }) {
  return (
    <>
      <div className={`subrow ${r.st}`}>
        <span className="mono crop-id">{r.id}</span>
        <Pill status={r.st} />
        <div className="trans-cell">
          {r.st === "complete" &&
          <div className="trans-preview">
              <span className="ts-sci">{r.sci}</span>
              <span className="ts-meta">{r.coll} · {r.date}</span>
            </div>
          }
          {r.st === "submitted" && <span className="hint mono" style={{ color: "var(--blue)" }}>extracting text…</span>}
          {r.st === "queued" && <span className="hint mono">— waiting —</span>}
          {r.st === "error" && <span className="hint" style={{ color: "var(--red-bright)" }}>{r.err}</span>}
        </div>
        <div className="subrow-act">
          {r.st === "complete" && <button className="btn btn-sm btn-ghost"><Icon paths={ICONS.eye} size={13} /> View</button>}
          {r.st === "error" &&
          <Region label="inline retry" pos="tr">
              <button className="btn btn-sm" data-comment-anchor="46ff929993-button-36-15"><Icon paths={ICONS.retry} size={13} /> Retry</button>
            </Region>
          }
        </div>
      </div>
    </>);

}

function VVGoSubmission() {
  return (
    <div className="submit">
      {/* Header / controls */}
      <Region n={1} label="submission controls — model, start batch" pos="tl" className="submit-top">
        <div className="row" style={{ gap: 16 }}>
          <div>
            <label className="label">Model</label>
            <div className="select-wrap" style={{ width: 280 }}>
              <select className="input sans">
                <option>gemini-3.1-flash-lite-preview</option>
                <option>gemini-3-flash-preview</option>
                <option>gemini-3-pro-preview</option>
              </select>
            </div>
          </div>
          <div>
            <label className="label">Prompt</label>
            <div className="select-wrap" style={{ width: 200 }}>
              <select className="input sans"><option>SLTPvM_default.yaml</option></select>
            </div>
          </div>
        </div>
        <Region n={2} label="start batch submission" pos="tr">
          <button className="btn btn-primary btn-lg"><Icon d={ICONS.send} size={15} /> Start Submission</button>
        </Region>
      </Region>

      {/* Progress */}
      <Region n={3} label="overall batch progress" pos="tl" className="progress-card panel">
        <div className="panel-body" data-comment-anchor="75c30a52d6-div-75-9">
          <div className="row between" style={{ marginBottom: 10 }}>
            <span className="mono" style={{ fontSize: 12.5, color: "var(--text-2)" }}>Submitting batch — 5 / 24 complete</span>
            <div className="row" style={{ gap: 14 }}>
              <span className="pill green"><span className="pdot" />5 complete</span>
              <span className="pill blue pulse"><span className="pdot" />2 submitting</span>
              <span className="pill red"><span className="pdot" />1 error</span>
              <span className="pill neutral"><span className="pdot" />16 queued</span>
            </div>
          </div>
          <div className="progress-track">
            <div className="progress-seg green" style={{ width: "21%" }} />
            <div className="progress-seg blue" style={{ width: "8%" }} />
            <div className="progress-seg red" style={{ width: "4%" }} />
          </div>
          <div className="row between" style={{ marginTop: 8 }}>
            <span className="hint">JSON results written to <span className="mono">~/output_packets/vvgo_results/</span></span>
            <span className="hint mono">ETA ≈ 4 min</span>
          </div>
        </div>
      </Region>

      {/* Table */}
      <Region n={4} label="per-crop submission status — queued / submitting / complete / error" pos="tl" className="subtable grow">
        <div className="panel" style={{ height: "100%", display: "flex", flexDirection: "column" }}>
          <div className="subtable-head">
            <span>Crop</span><span>Status</span>
            <Region n={5} label="transcription preview on complete" pos="bl" as="span" style={{ display: "block" }}>
              <span>Transcription preview</span>
            </Region>
            <span></span>
          </div>
          <div className="subtable-body">
            {SUB_ROWS.map((r) => <SubRow key={r.id} r={r} />)}
          </div>
        </div>
      </Region>
    </div>);

}

Object.assign(window, { VVGoSubmission });
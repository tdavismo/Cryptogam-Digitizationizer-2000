/* global React, Region, AnnoNote, Icon, ICONS, Placeholder, Field, ScanPreview */
const { useState: useStateS1 } = React;

/* Activity-log sample lines (shared by both variants) */
const LOG_LINES = [
{ t: "10:42:01", lvl: "ok", m: "Loaded 18 source images from /input_images" },
{ t: "10:42:02", lvl: "info", m: "Foreground = light · threshold = otsu · morph = 0.0015" },
{ t: "10:42:04", lvl: "info", m: "IMG_0241.tif → detecting packets…" },
{ t: "10:42:05", lvl: "ok", m: "IMG_0241.tif: found 12 packets, saved 12 crops" },
{ t: "10:42:07", lvl: "warn", m: "IMG_0242.tif: 1 crop oversize (3104×1870px) — possible merge" },
{ t: "10:42:09", lvl: "ok", m: "IMG_0243.tif: found 12 packets, saved 12 crops" },
{ t: "10:42:11", lvl: "err", m: "IMG_0244.tif: no packets detected — check foreground mode" },
{ t: "10:42:12", lvl: "info", m: "Writing packet_manifest.csv …" }];

function ActivityLog({ compact }) {
  return (
    <div className="log" style={{ height: compact ? "100%" : 196 }}>
      <div className="log-head">
        <span className="panel-title">Activity Log</span>
        <span className="mono" style={{ fontSize: 10, color: "var(--text-3)" }}>72 / 216 crops</span>
      </div>
      <div className="log-body">
        {LOG_LINES.map((l, i) =>
        <div className="log-line" key={i}>
            <span className="log-t">{l.t}</span>
            <span className={`log-lvl ${l.lvl}`}>
              {l.lvl === "ok" ? "OK" : l.lvl === "warn" ? "WARN" : l.lvl === "err" ? "ERR" : "··"}
            </span>
            <span className="log-m">{l.m}</span>
          </div>
        )}
        <div className="log-line">
          <span className="log-t">10:42:13</span>
          <span className="log-lvl ok">OK</span>
          <span className="log-m">Done. 14 images segmented · 2 flagged for review<span className="caret">▋</span></span>
        </div>
      </div>
    </div>);
}

/* Reusable config controls — used only by Variant A (full config rail) */
function ConfigFields({ rows, cols, setRows, setCols }) {
  return (
    <>
      <Region n={1} label="input folder" pos="tr">
        <Field label="Input folder" req>
          <div className="input-row">
            <input className="input" defaultValue="~/herbarium/input_images" />
            <button className="btn btn-icon" title="Browse"><Icon d={ICONS.folder} /></button>
          </div>
        </Field>
      </Region>

      <Region n={2} label="output folder" pos="tr">
        <Field label="Output folder" req>
          <div className="input-row">
            <input className="input" defaultValue="~/herbarium/output_packets" />
            <button className="btn btn-icon" title="Browse"><Icon d={ICONS.folder} /></button>
          </div>
        </Field>
      </Region>

      <Region n={3} label="VVGo API credentials" pos="tr">
        <Field label="VoucherVision Go — API token" req hint="Stored locally in ~/.cryptogam_config.json. Used at the submission step.">
          <div className="input-row">
            <input className="input" type="password" defaultValue="vvgo_sk_8841c2f0b7e4" />
            <button className="btn btn-icon" title="Show"><Icon paths={ICONS.eye} /></button>
          </div>
        </Field>
        <Field label="Model">
          <div className="select-wrap">
            <select className="input sans">
              <option>gemini-3.1-flash-lite-preview — fast · unlimited</option>
              <option>gemini-3-flash-preview — fast · good quality</option>
              <option>gemini-3-pro-preview — slow · highest quality</option>
            </select>
          </div>
        </Field>
      </Region>

      {/*
         Region 4 — Grid R×C retained in Variant A.
         Guides reading-order numbering/sort; does not constrain detection.
        */}
      <Region n={4} label="grid dimensions (rows × cols)" pos="tr">
        <Field label="Expected packet grid"
        hint="Guides reading-order sort. Detection still finds packets automatically.">
          <div className="row" style={{ gap: 8 }}>
            <div className="stepper">
              <button onClick={() => setRows(Math.max(1, rows - 1))}>−</button>
              <input className="input" value={rows} readOnly />
              <button onClick={() => setRows(rows + 1)}>+</button>
            </div>
            <span className="dim-x">×</span>
            <div className="stepper">
              <button onClick={() => setCols(Math.max(1, cols - 1))}>−</button>
              <input className="input" value={cols} readOnly />
              <button onClick={() => setCols(cols + 1)}>+</button>
            </div>
            <span className="hint" style={{ marginLeft: 4 }}>= {rows * cols} cells</span>
          </div>
        </Field>
      </Region>

      <Field label="Foreground">
        <div className="seg">
          <button className="on">Light</button>
          <button>Dark</button>
          <button>Auto</button>
        </div>
      </Field>

      <Field label="Padding — 30 px">
        <input type="range" className="slider" min="0" max="80" defaultValue="30" />
      </Field>
    </>);
}

/* ── Variant A: left config rail + preview + log ─────────────── */
function SessionSetupA({ rows, cols, setRows, setCols }) {
  return (
    <div className="setup-a">
      <div className="panel config-rail">
        <div className="panel-head">
          <span className="panel-title">Session Configuration</span>
          <span className="mono" style={{ fontSize: 10, color: "var(--text-3)" }}>step 1 / 4</span>
        </div>
        <div className="panel-body" style={{ overflowY: "auto" }}>
          <ConfigFields rows={rows} cols={cols} setRows={setRows} setCols={setCols} />
        </div>
        <div className="config-foot">
          <Region n={5} label="run segmentation" pos="tl">
            <button className="btn btn-primary btn-lg" style={{ width: "100%" }}>
              <Icon d={ICONS.play} size={15} /> Run Segmentation
            </button>
          </Region>
        </div>
      </div>

      <div className="setup-a-right">
        <Region n={6} label="source preview" pos="tr" className="grow" style={{ minHeight: 0 }}>
          <div style={{ position: "relative", width: "100%", height: "100%" }}>
            <ScanPreview
              rows={rows} cols={cols}
              flagged={{ 4: "gold", 7: "red" }}
              tag="IMG_0241.tif · 6240 × 4160 px"
              label={"[ source specimen scan ]\ndrop / load batch image here"}
              commentAnchor="93575ed8f9-div-105-5" />
            <AnnoNote style={{ top: 14, left: 14 }}>7 · detected packet bounding boxes, numbered in reading order</AnnoNote>
          </div>
        </Region>
        <Region n={8} label="activity log" pos="tl">
          <ActivityLog />
        </Region>
      </div>
    </div>);
}

/* ── Variant B: top config strip + dominant preview + right log ─ */
function SessionSetupB({ rows, cols, setRows, setCols }) {
  const [advOpen, setAdvOpen] = useStateS1(false);

  return (
    <div className="setup-b">
      <Region n={1} label="config strip — all session settings in one toolbar" pos="tl" className="config-strip">
        <div className="strip-grid">

          <div>
            <label className="label">Input folder</label>
            <div className="input-row">
              <input className="input" defaultValue="~/herbarium/input_images" />
              <button className="btn btn-icon"><Icon d={ICONS.folder} /></button>
            </div>
          </div>

          <div>
            <label className="label">Output folder</label>
            <div className="input-row">
              <input className="input" defaultValue="~/herbarium/output_packets" />
              <button className="btn btn-icon"><Icon d={ICONS.folder} /></button>
            </div>
          </div>

          {/* Grid R×C — VVGo token moved to step 04 where it is actually used */}
          <div>
            <label className="label">Grid R × C</label>
            <div className="row" style={{ gap: 6 }}>
              <div className="stepper">
                <button onClick={() => setRows(Math.max(1, rows - 1))}>−</button>
                <input className="input" value={rows} readOnly />
                <button onClick={() => setRows(rows + 1)}>+</button>
              </div>
              <span className="dim-x">×</span>
              <div className="stepper">
                <button onClick={() => setCols(Math.max(1, cols - 1))}>−</button>
                <input className="input" value={cols} readOnly />
                <button onClick={() => setCols(cols + 1)}>+</button>
              </div>
            </div>
          </div>

          <div data-comment-anchor="00c1c9145e-div-200-11" className="adv-trigger-wrap">
            <label className="label">Processing</label>
            <button
              className={`btn${advOpen ? " adv-open" : ""}`}
              onClick={() => setAdvOpen((v) => !v)}>
              <Icon d={ICONS.filter} size={13} />
              Advanced {advOpen ? "▴" : "▾"}
            </button>

            {advOpen &&
            <div className="adv-panel" data-comment-anchor="5dbb00988b-div-212-15">
                <span className="adv-panel-title">Advanced Settings</span>
                <div className="adv-field">
                  <label className="label">Threshold</label>
                  <div className="seg" style={{ width: "100%" }}>
                    <button className="on">Otsu</button>
                    <button>Fixed</button>
                    <button>Adaptive</button>
                  </div>
                </div>
                <div className="adv-field">
                  <label className="label">Deskew</label>
                  <div className="seg">
                    <button className="on">On</button>
                    <button>Off</button>
                  </div>
                </div>
                <div className="adv-field" style={{ gridColumn: "1 / -1" }}>
                  <label className="label">Contrast — 100%</label>
                  <input type="range" className="slider" min="0" max="200" defaultValue="100" />
                </div>
                <div className="adv-field" style={{ gridColumn: "1 / -1" }}>
                  <label className="label">Padding — 30 px</label>
                  <input type="range" className="slider" min="0" max="80" defaultValue="30" />
                </div>
              </div>
            }
          </div>

          <div style={{ display: "flex", alignItems: "flex-end" }}>
            <Region n={5} label="run" pos="tl" style={{ width: "100%" }}>
              <button className="btn btn-primary btn-lg" style={{ width: "100%" }}>
                <Icon d={ICONS.play} size={15} /> Run
              </button>
            </Region>
          </div>

        </div>
      </Region>

      <div className="setup-b-main">
        <Region n={6} label="preview — maximised" pos="tr" className="grow">
          <div style={{ position: "relative", width: "100%", height: "100%" }}>
            <ScanPreview
              rows={rows} cols={cols}
              flagged={{ 4: "gold", 7: "red" }}
              tag="IMG_0241.tif · 6240 × 4160 px"
              label={"[ source specimen scan ]\ndrop / load batch image here"} />
            <AnnoNote style={{ top: 14, left: 14 }}>7 · detected packet bounding boxes</AnnoNote>
          </div>
        </Region>
        <Region n={8} label="activity log — docked right rail" pos="tr" className="setup-b-log">
          <ActivityLog compact />
        </Region>
      </div>
    </div>);
}

function SessionSetup({ variant }) {
  const [rows, setRows] = useStateS1(4);
  const [cols, setCols] = useStateS1(3);
  const props = { rows, cols, setRows, setCols };
  return variant === "B" ? <SessionSetupB {...props} /> : <SessionSetupA {...props} />;
}

Object.assign(window, { SessionSetup });
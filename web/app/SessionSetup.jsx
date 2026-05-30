/* global React, Region, AnnoNote, Icon, ICONS, Placeholder, Field, ScanPreview */
const { useState: useStateS1, useRef: useRefS1 } = React;

/* ── Live API wiring ──────────────────────────────────────────────────────
   The frontend is served by the FastAPI backend (same origin). /api/batch
   streams Server-Sent Events; we parse them off the fetch body reader so the
   activity log and counters update in real time. */

const _now = () => new Date().toLocaleTimeString("en-GB", { hour12: false });

async function _streamBatch(body, onEvent, signal) {
  const res = await fetch("/api/batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
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
      if (line) onEvent(JSON.parse(line.slice(5).trim()));
    }
  }
}

/* Fallback sample lines shown before the first run (preserves the design look) */
const LOG_LINES = [
{ t: "10:42:01", lvl: "ok", m: "Loaded 18 source images from /input_images" },
{ t: "10:42:02", lvl: "info", m: "Foreground = light · threshold = otsu · morph = 0.0015" },
{ t: "10:42:04", lvl: "info", m: "IMG_0241.tif → detecting packets…" },
{ t: "10:42:05", lvl: "ok", m: "IMG_0241.tif: found 12 packets, saved 12 crops" },
{ t: "10:42:07", lvl: "warn", m: "IMG_0242.tif: 1 crop oversize (3104×1870px) — possible merge" },
{ t: "10:42:09", lvl: "ok", m: "IMG_0243.tif: found 12 packets, saved 12 crops" },
{ t: "10:42:11", lvl: "err", m: "IMG_0244.tif: no packets detected — check foreground mode" },
{ t: "10:42:12", lvl: "info", m: "Writing packet_manifest.csv …" }];

function ActivityLog({ compact, lines, summary, live }) {
  const data = lines && lines.length ? lines : LOG_LINES;
  return (
    <div className="log" style={{ height: compact ? "100%" : 196 }}>
      <div className="log-head">
        <span className="panel-title">Activity Log</span>
        <span className="mono" style={{ fontSize: 10, color: "var(--text-3)" }}>
          {summary || "72 / 216 crops"}
        </span>
      </div>
      <div className="log-body">
        {data.map((l, i) =>
        <div className="log-line" key={i}>
            <span className="log-t">{l.t}</span>
            <span className={`log-lvl ${l.lvl}`}>
              {l.lvl === "ok" ? "OK" : l.lvl === "warn" ? "WARN" : l.lvl === "err" ? "ERR" : "··"}
            </span>
            <span className="log-m">{l.m}</span>
          </div>
        )}
        {!lines &&
        <div className="log-line">
          <span className="log-t">10:42:13</span>
          <span className="log-lvl ok">OK</span>
          <span className="log-m">Done. 14 images segmented · 2 flagged for review<span className="caret">▋</span></span>
        </div>
        }
        {live &&
        <div className="log-line">
          <span className="log-t" />
          <span className="log-lvl" />
          <span className="log-m"><span className="caret">▋</span></span>
        </div>
        }
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

/* ── Segmented button group (single-select) ──────────────────── */
function Seg({ value, options, onChange }) {
  return (
    <div className="seg" style={{ width: "100%" }}>
      {options.map((o) =>
        <button key={o.v}
          className={value === o.v ? "on" : ""}
          onClick={() => onChange(o.v)}>{o.label}</button>
      )}
    </div>);
}

/* ── Live preview — real source image + detected bounding boxes ──
   Boxes arrive from /api/batch in full-image pixel coords. The <img>
   (object-fit: contain) and the SVG overlay (viewBox = image dims,
   preserveAspectRatio xMidYMid meet) letterbox identically, so the
   polygons land exactly on the packets. Box styling reuses the same
   .bb / .bb-ok classes as the aesthetic ScanPreview overlay. */
function LivePreview({ preview, fallbackRows, fallbackCols }) {
  if (!preview || !preview.src) {
    return (
      <ScanPreview
        rows={fallbackRows} cols={fallbackCols}
        flagged={{}}
        tag="awaiting run…"
        label={"[ source specimen scan ]\nrun segmentation to load live boxes"} />);
  }
  const { src, boxes, iw, ih, name } = preview;
  const fontPx = Math.max(11, Math.round(ih * 0.016));
  return (
    <div className="scan-photo" style={{ position: "relative" }}>
      <img src={src} alt={name}
        style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }} />
      <svg viewBox={`0 0 ${iw} ${ih}`} preserveAspectRatio="xMidYMid meet"
        style={{ position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none" }}>
        {(boxes || []).map((b, i) => {
          const pts = [[b.x, b.y], [b.x + b.w, b.y], [b.x + b.w, b.y + b.h], [b.x, b.y + b.h]]
            .map((p) => p.join(",")).join(" ");
          return (
            <g key={i} className="bb bb-ok">
              <polygon points={pts} strokeLinejoin="round" />
              <text x={b.x + 5} y={b.y + fontPx + 3} fontSize={fontPx}
                fontFamily="var(--bb-font, 'IBM Plex Mono', monospace)"
                fontWeight="600" opacity="0.9">{String(b.idx).padStart(2, "0")}</text>
            </g>);
        })}
      </svg>
      <span className="ph-tag">{name} · {(boxes || []).length} packets</span>
    </div>);
}

/* ── Variant B: top config strip + dominant preview + right log ─
   This is the locked production variant — wired to the live API. */
function SessionSetupB({ rows, cols, setRows, setCols }) {
  const [advOpen, setAdvOpen] = useStateS1(false);
  const [folder, setFolder] = useStateS1("");
  const [out, setOut] = useStateS1("");
  const [running, setRunning] = useStateS1(false);
  const [logLines, setLogLines] = useStateS1(null);   // null → show design sample
  const [summary, setSummary] = useStateS1(null);
  const [preview, setPreview] = useStateS1(null);     // {src,boxes,iw,ih,name}
  const abortRef = useRefS1(null);

  /* Advanced settings — mirror the original desktop app's SegSettings */
  const [foreground, setForeground] = useStateS1("light");
  const [threshold, setThreshold]   = useStateS1("otsu");
  const [contrast, setContrast]     = useStateS1("none");
  const [deskew, setDeskew]         = useStateS1(true);
  const [topCrop, setTopCrop]       = useStateS1(0);     // %
  const [padding, setPadding]       = useStateS1(30);    // px
  const [minArea, setMinArea]       = useStateS1(0.05);  // %

  const addLog = (lvl, m) =>
    setLogLines((prev) => [...(prev || []), { t: _now(), lvl, m }]);

  function buildSettings() {
    return {
      foreground,
      threshold_mode: threshold,
      contrast,
      deskew,
      padding: Number(padding),
      top_crop_frac: Number(topCrop) / 100,
      min_area_frac: Number(minArea) / 100,
    };
  }

  async function run() {
    if (running) {                       // acts as Stop while a batch streams
      if (abortRef.current) abortRef.current.abort();
      return;
    }
    if (!folder.trim()) {
      setLogLines([{ t: _now(), lvl: "err", m: "No source folder set — type or paste an absolute path." }]);
      setSummary("0 crops");
      return;
    }

    setLogLines([{ t: _now(), lvl: "info", m: `Starting segmentation · source ${folder}` }]);
    setSummary("starting…");
    setRunning(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    let packets = 0;
    let outputDir = null;

    try {
      await _streamBatch(
        {
          input_dir: folder,
          output_dir: out.trim() ? out.trim() : null,
          settings: buildSettings(),
        },
        (evt) => {
          if (evt.type === "start") {
            outputDir = evt.output_dir;
            if (!out.trim()) setOut(outputDir);
            addLog("ok", `Loaded ${evt.total} source images → ${outputDir}`);
            setSummary(`0 / ${evt.total} images`);
          } else if (evt.type === "progress") {
            packets += evt.count;
            addLog(evt.count ? "ok" : "warn",
              `${evt.name}: ${evt.count} packet${evt.count === 1 ? "" : "s"}`);
            setSummary(`${evt.i} / ${evt.total} images · ${packets} crops`);
            /* Show the just-processed image with its detected boxes */
            setPreview({
              src: "/api/file?path=" + encodeURIComponent(evt.path),
              boxes: evt.boxes, iw: evt.iw, ih: evt.ih, name: evt.name,
            });
          } else if (evt.type === "error") {
            addLog("err", `${evt.name}: ${evt.error}`);
          } else if (evt.type === "done") {
            const oversize = evt.flagged.filter((f) => f.flag === "oversize").length;
            const zero = evt.flagged.filter((f) => f.flag === "none").length;
            addLog("ok",
              `Done. ${packets} packets across ${evt.total} images · ${evt.flagged.length} flagged`);
            if (evt.flagged.length)
              addLog("warn", `${oversize} oversize · ${zero} zero-detection — see QC Review`);
            setSummary(`${packets} crops · ${evt.flagged.length} flagged`);
            window.__CDZ_SESSION = {
              outputDir, flagged: evt.flagged, sources: evt.total, packets,
              settings: buildSettings(),
            };
          }
        },
        ctrl.signal,
      );
    } catch (err) {
      if (err.name === "AbortError") addLog("warn", "Stopped by user.");
      else addLog("err", err.message);
    } finally {
      setRunning(false);
      abortRef.current = null;
    }
  }

  return (
    <div className="setup-b">
      <div className="config-strip">
        <div className="strip-grid">

          <div>
            <label className="label">Input folder</label>
            <div className="input-row">
              <input className="input" value={folder}
                placeholder="~/herbarium/input_images"
                onChange={(e) => setFolder(e.target.value)} />
              <button className="btn btn-icon" title="Browse"
                onClick={() => { const p = window.prompt("Paste the absolute path to the source folder:", folder); if (p && p.trim()) setFolder(p.trim()); }}>
                <Icon d={ICONS.folder} />
              </button>
            </div>
          </div>

          <div>
            <label className="label">Output folder</label>
            <div className="input-row">
              <input className="input" value={out}
                placeholder="(auto · dated folder in source)"
                onChange={(e) => setOut(e.target.value)} />
              <button className="btn btn-icon" title="Browse"
                onClick={() => { const p = window.prompt("Paste the absolute path to the output folder:", out); if (p && p.trim()) setOut(p.trim()); }}>
                <Icon d={ICONS.folder} />
              </button>
            </div>
          </div>

          {/* Grid R×C — guides reading-order numbering; does not constrain detection */}
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

          <div className="adv-trigger-wrap">
            <label className="label">Processing</label>
            <button
              className={`btn${advOpen ? " adv-open" : ""}`}
              onClick={() => setAdvOpen((v) => !v)}>
              <Icon d={ICONS.filter} size={13} />
              Advanced {advOpen ? "▴" : "▾"}
            </button>

            {advOpen &&
            <div className="adv-panel">
                <span className="adv-panel-title">Advanced Settings</span>

                <div className="adv-field">
                  <label className="label">Foreground</label>
                  <Seg value={foreground} onChange={setForeground}
                    options={[{ v: "light", label: "Light" }, { v: "dark", label: "Dark" }, { v: "auto", label: "Auto" }]} />
                </div>

                <div className="adv-field">
                  <label className="label">Threshold</label>
                  <Seg value={threshold} onChange={setThreshold}
                    options={[{ v: "otsu", label: "Otsu" }, { v: "adaptive", label: "Adaptive" }, { v: "canny", label: "Canny" }]} />
                </div>

                <div className="adv-field">
                  <label className="label">Contrast</label>
                  <div className="select-wrap">
                    <select className="input sans" value={contrast}
                      onChange={(e) => setContrast(e.target.value)}>
                      <option value="none">None</option>
                      <option value="normalize">Normalize</option>
                      <option value="clahe">CLAHE</option>
                      <option value="both">Both</option>
                    </select>
                  </div>
                </div>

                <div className="adv-field">
                  <label className="label">Deskew</label>
                  <Seg value={deskew ? "on" : "off"} onChange={(v) => setDeskew(v === "on")}
                    options={[{ v: "on", label: "On" }, { v: "off", label: "Off" }]} />
                </div>

                <div className="adv-field" style={{ gridColumn: "1 / -1" }}>
                  <label className="label">Top crop — {topCrop}%</label>
                  <input type="range" className="slider" min="0" max="30" step="1"
                    value={topCrop} onChange={(e) => setTopCrop(+e.target.value)} />
                </div>

                <div className="adv-field" style={{ gridColumn: "1 / -1" }}>
                  <label className="label">Padding — {padding} px</label>
                  <input type="range" className="slider" min="0" max="80" step="1"
                    value={padding} onChange={(e) => setPadding(+e.target.value)} />
                </div>

                <div className="adv-field" style={{ gridColumn: "1 / -1" }}>
                  <label className="label">Min area — {(+minArea).toFixed(2)}%</label>
                  <input type="range" className="slider" min="0.01" max="2" step="0.01"
                    value={minArea} onChange={(e) => setMinArea(+e.target.value)} />
                </div>
              </div>
            }
          </div>

          <div style={{ display: "flex", alignItems: "flex-end" }}>
            <button className="btn btn-primary btn-lg" style={{ width: "100%" }}
              onClick={run}>
              <Icon d={running ? ICONS.x : ICONS.play} size={15} /> {running ? "Stop" : "Run"}
            </button>
          </div>

        </div>
      </div>

      <div className="setup-b-main">
        <div className="setup-b-preview" style={{ flex: 1, minWidth: 0 }}>
          <LivePreview preview={preview} fallbackRows={rows} fallbackCols={cols} />
        </div>
        <div className="setup-b-log">
          <ActivityLog compact lines={logLines} summary={summary} live={running} />
        </div>
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

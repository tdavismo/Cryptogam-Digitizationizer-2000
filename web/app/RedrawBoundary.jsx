/* global React, Icon, ICONS, Placeholder, ScanPreview */
const { useState: useStateS3, useEffect: useEffectS3, useMemo: useMemoS3, useRef: useRefS3 } = React;

/* ── Persistent screen state ─────────────────────────────────────────────
   Mirrors the pattern used by other screens — selected crop survives tab
   switches.  The full manifest is re-fetched on mount; it's cheap and
   guarantees we pick up any redraws made through the QC screen.            */
if (!window.__CDZ_REDRAW_STATE) {
  window.__CDZ_REDRAW_STATE = { idx: 0 };
}
const RS = window.__CDZ_REDRAW_STATE;

/* Pick a roughly-square grid for the source-context minimap.
   The exact rows/cols of a real specimen layout aren't known, but matching
   the per-source packet count makes the "active" highlight read correctly. */
function _gridFor(n) {
  if (n <= 1) return { rows: 1, cols: 1 };
  const cols = Math.max(1, Math.ceil(Math.sqrt(n * 1.3)));
  const rows = Math.max(1, Math.ceil(n / cols));
  return { rows, cols };
}

/* Compute the "auto-zoom" view region around a detection box.
   We want to show the box itself plus enough context that the volunteer can
   see what's adjacent (other packets, the edge of the mat, etc.).
   Returns {vx, vy, vw, vh, scale} where scale converts source-px to
   container-px and (vx, vy) is the view origin in source pixels. */
function _viewRegion(box, src, container, marginFrac = 0.35) {
  const { x, y, w, h } = box;
  const { source_w: sw, source_h: sh } = src;
  const { cw, ch } = container;
  if (!sw || !sh || !cw || !ch) return null;

  // Padded view region around the box, centred on its midpoint
  const mw = w * marginFrac, mh = h * marginFrac;
  let vw = w + mw * 2;
  let vh = h + mh * 2;
  // Inflate to match the container aspect so the image fills the editor
  const cAR = cw / ch, vAR = vw / vh;
  if (vAR > cAR) vh = vw / cAR; else vw = vh * cAR;
  // Clamp to source bounds, keeping the box centred when possible
  vw = Math.min(vw, sw); vh = Math.min(vh, sh);
  let vx = (x + w / 2) - vw / 2;
  let vy = (y + h / 2) - vh / 2;
  vx = Math.max(0, Math.min(sw - vw, vx));
  vy = Math.max(0, Math.min(sh - vh, vy));

  const scale = cw / vw;          // px-on-screen per px-in-source
  return { vx, vy, vw, vh, scale };
}

function Handle({ pos }) { return <span className={`bbox-handle ${pos}`} />; }

/* Empty-state card (same shape as the QC / VVGo empty states). */
function RedrawEmpty({ msg }) {
  return (
    <div className="qc-empty">
      <div className="qc-empty-card">
        <Icon paths={ICONS.warn} size={28} />
        <div className="qc-empty-title">{msg.title}</div>
        <div className="qc-empty-sub">{msg.body}</div>
      </div>
    </div>);
}

function RedrawBoundary() {
  const session = window.__CDZ_SESSION || null;

  /* manifest: { crop_path: {source_path, source_w, source_h, x, y, w, h, ...} } */
  const [manifest, setManifest] = useStateS3(null);
  const [loadErr,  setLoadErr]  = useStateS3(null);
  const [idx,      setIdx]      = useStateS3(RS.idx);

  /* Editor canvas dimensions — needed to compute the auto-zoom scale.
     Uses a *stable* callback ref (memoised once for the component's lifetime)
     so the ResizeObserver subscribes the moment the editor div mounts and is
     not torn down on every render.  A fresh-each-render callback ref would
     loop infinitely: React would call it after every commit, the synchronous
     setContainer below would trigger another render, repeat. */
  const [container, setContainer] = useStateS3({ cw: 0, ch: 0 });
  const observerRef = useRefS3(null);
  const canvasRef = useMemoS3(() => (el) => {
    if (observerRef.current) { observerRef.current.disconnect(); observerRef.current = null; }
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const r = el.getBoundingClientRect();
      setContainer((prev) => {
        const cw = Math.round(r.width), ch = Math.round(r.height);
        return prev.cw === cw && prev.ch === ch ? prev : { cw, ch };
      });
    });
    ro.observe(el);
    observerRef.current = ro;
    /* Synchronously seed an initial size — ResizeObserver's first callback
       can lag a frame, which would leave `view` null on the first paint. */
    const r = el.getBoundingClientRect();
    if (r.width && r.height) {
      setContainer((prev) => {
        const cw = Math.round(r.width), ch = Math.round(r.height);
        return prev.cw === cw && prev.ch === ch ? prev : { cw, ch };
      });
    }
  }, []);

  useEffectS3(() => { RS.idx = idx; }, [idx]);

  /* Load manifest on first mount + whenever the active batch changes */
  useEffectS3(() => {
    if (!session || !session.outputDir) return;
    setLoadErr(null);
    fetch("/api/manifest?output_dir=" + encodeURIComponent(session.outputDir))
      .then((r) => r.ok ? r.json() : r.json().then((j) => Promise.reject(j.detail || r.statusText)))
      .then((j) => setManifest(j))
      .catch((e) => setLoadErr(String(e)));
  }, [session && session.outputDir]);

  /* Stable list of crop paths (manifest keys) — sorted so Prev/Next is
     deterministic across reloads. */
  const cropPaths = useMemoS3(
    () => manifest ? Object.keys(manifest.crops).sort() : [],
    [manifest]);

  const curPath = cropPaths[Math.max(0, Math.min(idx, cropPaths.length - 1))];
  const cur     = curPath && manifest ? manifest.crops[curPath] : null;

  /* Per-source siblings — used to build the minimap layout */
  const siblings = useMemoS3(() => {
    if (!cur || !manifest) return [];
    return cropPaths
      .map((p) => ({ p, m: manifest.crops[p] }))
      .filter(({ m }) => m.source_path === cur.source_path)
      .sort((a, b) => a.m.packet_index - b.m.packet_index);
  }, [cur, manifest, cropPaths]);

  const sibIndex = siblings.findIndex((s) => s.p === curPath);
  const grid     = _gridFor(siblings.length || 1);

  /* Auto-zoom math */
  const view = useMemoS3(
    () => (cur && container.cw ? _viewRegion(cur, cur, container) : null),
    [cur, container.cw, container.ch]);

  function goto(n) { setIdx(Math.max(0, Math.min(cropPaths.length - 1, n))); }

  /* ── Empty / loading states ──────────────────────────────────────────── */
  if (!session)
    return <RedrawEmpty msg={{ title: "No batch loaded",
      body: <>Run a segmentation on the <b>Session Setup</b> tab to populate the editor.</> }} />;
  if (loadErr)
    return <RedrawEmpty msg={{ title: "Could not load manifest", body: loadErr }} />;
  if (!manifest)
    return <RedrawEmpty msg={{ title: "Loading…", body: "Reading packet_manifest.csv" }} />;
  if (cropPaths.length === 0)
    return <RedrawEmpty msg={{ title: "Manifest is empty",
      body: "No crops to redraw." }} />;

  /* ── Editor render ───────────────────────────────────────────────────── */
  // Source <img> placement: shown at full source size scaled by `scale`, then
  // translated so the view-region origin is at the canvas origin.
  const imgStyle = view ? {
    position: "absolute", left: 0, top: 0,
    width: cur.source_w * view.scale,
    height: cur.source_h * view.scale,
    transform: `translate(${-view.vx * view.scale}px, ${-view.vy * view.scale}px)`,
    pointerEvents: "none", userSelect: "none",
    maxWidth: "none", maxHeight: "none",
  } : { display: "none" };

  // Bbox placement — same transform space as the image
  const boxStyle = view ? {
    position: "absolute",
    left:   (cur.x - view.vx) * view.scale,
    top:    (cur.y - view.vy) * view.scale,
    width:  cur.w * view.scale,
    height: cur.h * view.scale,
  } : { display: "none" };

  return (
    <div className="redraw">
      <div className="redraw-main">

        {/* Editor canvas */}
        <div className="editor-wrap grow" style={{ minWidth: 0 }}>
          <div ref={canvasRef} className="editor-canvas"
               style={{ position: "relative", width: "100%", height: "100%",
                        overflow: "hidden" }}>
            {view && <>
              <img src={"/api/file?path=" + encodeURIComponent(cur.source_path)}
                   alt="" style={imgStyle} draggable={false} />
              <div className="bbox" style={boxStyle}>
                {["nw","n","ne","e","se","s","sw","w"].map(
                  (h) => <Handle key={h} pos={h} />)}
                <span className="bbox-dim">{cur.w} × {cur.h} px</span>
              </div>
            </>}
            <span className="ph-label" style={{ position: "absolute", bottom: 12, left: 14,
                                                 textAlign: "left",
                                                 color: "var(--text-3)",
                                                 fontFamily: "var(--mono)",
                                                 fontSize: 10,
                                                 pointerEvents: "none" }}>
              auto-zoomed to packet {String(cur ? cur.packet_index : 0).padStart(2, "0")} · full-resolution source region
            </span>
          </div>
        </div>

        {/* Right rail */}
        <div className="redraw-rail">

          <div className="panel">
            <div className="panel-head"><span className="panel-title">Source Context</span></div>
            <div className="panel-body">
              <div style={{ height: 168, position: "relative" }}>
                <ScanPreview rows={grid.rows} cols={grid.cols}
                  activeIndex={sibIndex >= 0 ? sibIndex : 0}
                  tag={cur ? cur.source_path.split(/[\\/]/).pop() : ""} />
              </div>
              <div className="hint" style={{ marginTop: 9 }}>
                Editing crop <b style={{ color: "var(--text)" }}>
                  {String(cur ? cur.packet_index : 0).padStart(2, "0")}
                </b> · highlighted box shows the current region within the full source scan.
              </div>
            </div>
          </div>

          <div className="panel">
            <div className="panel-body row between">
              <button className="btn btn-sm"
                onClick={() => goto(idx - 1)}
                disabled={idx <= 0}>
                <Icon d={ICONS.arrowLeft} size={15} /> Prev
              </button>
              <span className="mono" style={{ fontSize: 12, color: "var(--text-2)" }}>
                crop {idx + 1} / {cropPaths.length}
              </span>
              <button className="btn btn-sm"
                onClick={() => goto(idx + 1)}
                disabled={idx >= cropPaths.length - 1}>
                Next <Icon d={ICONS.arrowRight} size={15} />
              </button>
            </div>
          </div>

          <div className="redraw-coords panel">
            <div className="panel-body">
              <div className="meta-grid">
                <span className="mk">X · Y</span>
                <span className="mv">{cur ? cur.x : "—"} · {cur ? cur.y : "—"}</span>
                <span className="mk">W · H</span>
                <span className="mv">{cur ? cur.w : "—"} · {cur ? cur.h : "—"}</span>
                <span className="mk">Padding</span>
                <span className="mv">{cur && cur.padding != null ? cur.padding + " px" : "(deskewed)"}</span>
              </div>
            </div>
          </div>

          <div className="redraw-actions">
            <button className="btn btn-primary btn-lg" style={{ flex: 1 }} disabled>
              <Icon d={ICONS.check} size={15} /> Accept Boundary
            </button>
            <button className="btn btn-lg" title="Reset to detected box" disabled>
              <Icon paths={ICONS.reset} size={15} />
            </button>
          </div>
          <div className="hint" style={{ textAlign: "center" }}>
            Drag interactions arrive in Phase C
          </div>

        </div>
      </div>
    </div>
  );
}

Object.assign(window, { RedrawBoundary });

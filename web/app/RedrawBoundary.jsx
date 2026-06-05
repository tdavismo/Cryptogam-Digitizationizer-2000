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

function Handle({ pos, onDrag }) {
  /* Stop the mousedown from bubbling to the bbox body (which would start a
     "move" drag) and start a "resize" drag on this handle instead. */
  return <span className={`bbox-handle ${pos}`}
    onMouseDown={(e) => { e.stopPropagation(); onDrag(e, "resize", pos); }} />;
}

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

  /* When QC hands off a specific crop (RS.path), resolve it to *this* list's
     index once the manifest is loaded — robust to any sort-order difference
     between the two screens. Consume RS.path so it doesn't re-fire on later
     manifest reloads. */
  useEffectS3(() => {
    if (!cropPaths.length || !RS.path) return;
    const i = cropPaths.indexOf(RS.path);
    RS.path = null;
    if (i >= 0) setIdx(i);
  }, [cropPaths]);

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

  /* Minimap layout — use the real R×C the user set on Session Setup so the
     context grid matches the actual specimen layout (a 4×2 capture shows as
     4 rows × 2 cols, not a rotated guess). Fall back to a square-ish grid only
     when the session didn't record a layout. */
  const grid = (() => {
    const r = session && session.gridRows, c = session && session.gridCols;
    if (typeof r === "number" && typeof c === "number" && r * c >= siblings.length) {
      return { rows: r, cols: c };
    }
    return _gridFor(siblings.length || 1);
  })();

  /* Per-cell QC status colour for the minimap. Siblings are packet-index
     sorted, so sibling array-index j maps to ScanPreview cell j (cell i shows
     packet i+1). We pull each crop's QC override (approved/flagged) from the
     shared QC state, falling back to the manifest-derived status. */
  const qcOverrides = (window.__CDZ_QC_STATE && window.__CDZ_QC_STATE.overrides) || {};
  const STATUS_TO_BB = { approved: "green", flagged: "red", none: "red", oversize: "gold" };
  const miniFlags = {};
  siblings.forEach((sib, j) => {
    const ov = qcOverrides[sib.p];
    const col = STATUS_TO_BB[ov];
    if (col) miniFlags[j] = col;
  });

  /* draftBox is the user's in-progress redraw — overrides the manifest box
     until Accept (POST /api/redraw + manifest mutation) or Reset (discard). */
  const [draftBox,  setDraftBox]  = useStateS3(null);
  const [flash,     setFlash]     = useStateS3(null);   // "saved" | "failed:..."
  const [saving,    setSaving]    = useStateS3(false);
  const dragRef = useRefS3(null);

  /* Pan/zoom of the editor viewport. `pan` shifts the visible region in
     source pixels; `zoom` multiplies the auto-zoom (1 = fit-to-box). Both
     reset whenever the current crop changes (handled in goto + an effect). */
  const [pan,  setPan]  = useStateS3({ dx: 0, dy: 0 });
  const [zoom, setZoom] = useStateS3(1);
  const panRef = useRefS3(null);

  /* The "effective" box (draft if set, else the manifest's) drives every
     position / readout. */
  const effective = draftBox || (cur
    ? { x: cur.x, y: cur.y, w: cur.w, h: cur.h }
    : null);

  /* Auto-zoom base region around the manifest box, then apply user pan/zoom.
     Zoom shrinks the region about its centre; pan translates it; both are
     clamped to the source bounds so you can't scroll off the image. */
  const view = useMemoS3(() => {
    if (!cur || !container.cw) return null;
    const base = _viewRegion(cur, cur, container);
    if (!base) return null;
    const sw = cur.source_w, sh = cur.source_h;
    let vw = base.vw / zoom, vh = base.vh / zoom;
    vw = Math.min(vw, sw); vh = Math.min(vh, sh);
    const cx = base.vx + base.vw / 2 + pan.dx;
    const cy = base.vy + base.vh / 2 + pan.dy;
    let vx = cx - vw / 2, vy = cy - vh / 2;
    vx = Math.max(0, Math.min(sw - vw, vx));
    vy = Math.max(0, Math.min(sh - vh, vy));
    return { vx, vy, vw, vh, scale: container.cw / vw };
  }, [cur, container.cw, container.ch, pan, zoom]);

  /* Reset pan/zoom when the crop changes. */
  useEffectS3(() => { setPan({ dx: 0, dy: 0 }); setZoom(1); }, [curPath]);

  /* Navigation clears any in-progress draft. */
  function goto(n) {
    setDraftBox(null);
    setIdx(Math.max(0, Math.min(cropPaths.length - 1, n)));
  }

  /* ── Pan: drag on empty canvas (not on the bbox) moves the viewport. ── */
  function startPan(e) {
    if (!view || saving) return;
    e.preventDefault();
    panRef.current = { mx: e.clientX, my: e.clientY, pan };
    const onMove = (ev) => {
      const p = panRef.current; if (!p) return;
      /* Drag right → image moves right → view origin moves left, hence the
         negative sign. Convert screen-px delta to source-px via scale. */
      setPan({
        dx: p.pan.dx - (ev.clientX - p.mx) / view.scale,
        dy: p.pan.dy - (ev.clientY - p.my) / view.scale,
      });
    };
    const onUp = () => {
      panRef.current = null;
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  /* Wheel zoom, clamped 1×–8×. */
  function onWheel(e) {
    if (!view) return;
    e.preventDefault();
    setZoom((z) => Math.max(1, Math.min(8, z * (e.deltaY < 0 ? 1.15 : 1 / 1.15))));
  }

  /* ── Drag interactions ────────────────────────────────────────────────
     Single state machine: mousedown anywhere on the bbox starts either a
     "move" (body) or "resize" (handle) drag. Mousemove / mouseup listeners
     attach to the window so the cursor can leave the editor without losing
     the drag. Delta is in editor px, converted to source px via view.scale. */
  function startDrag(e, mode, handle) {
    if (!cur || !view || saving) return;
    e.preventDefault();
    const start = effective;
    dragRef.current = { mode, handle, mx: e.clientX, my: e.clientY, box: start };

    const MIN = 20;     // minimum bbox edge length (source px) to keep useable
    const sw = cur.source_w, sh = cur.source_h;

    const onMove = (ev) => {
      const d = dragRef.current; if (!d) return;
      const dx = (ev.clientX - d.mx) / view.scale;
      const dy = (ev.clientY - d.my) / view.scale;
      let { x, y, w, h } = d.box;
      if (d.mode === "move") {
        x += dx; y += dy;
      } else {
        const h_ = d.handle;
        if (h_.includes("n")) { y += dy; h -= dy; }
        if (h_.includes("s")) { h += dy; }
        if (h_.includes("w")) { x += dx; w -= dx; }
        if (h_.includes("e")) { w += dx; }
      }
      /* Enforce min size before clamping position so dragging a left edge
         past the right edge doesn't flip the box. */
      if (w < MIN) { if (d.mode === "resize" && d.handle.includes("w")) x = d.box.x + d.box.w - MIN; w = MIN; }
      if (h < MIN) { if (d.mode === "resize" && d.handle.includes("n")) y = d.box.y + d.box.h - MIN; h = MIN; }
      /* Clamp to source bounds, preserving size where possible (move) and
         shrinking only when we hit an edge during a resize. */
      if (d.mode === "move") {
        x = Math.max(0, Math.min(sw - w, x));
        y = Math.max(0, Math.min(sh - h, y));
      } else {
        if (x < 0)         { w += x; x = 0; }
        if (y < 0)         { h += y; y = 0; }
        if (x + w > sw)    { w = sw - x; }
        if (y + h > sh)    { h = sh - y; }
        w = Math.max(MIN, w);
        h = Math.max(MIN, h);
      }
      setDraftBox({ x: Math.round(x), y: Math.round(y),
                    w: Math.round(w), h: Math.round(h) });
    };
    const onUp = () => {
      dragRef.current = null;
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  /* ── Accept — POST /api/redraw, mutate local manifest, advance ─────── */
  async function accept() {
    if (!draftBox || !cur || saving) return;
    setSaving(true);
    try {
      const r = await fetch("/api/redraw", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_path: cur.source_path,
          output_path: curPath,
          x: draftBox.x, y: draftBox.y, w: draftBox.w, h: draftBox.h,
          /* Re-apply the original padding so the saved file matches the
             original batch's geometry. Deskewed rows lose this info; fall
             back to the standard 30 px the batch defaults to. */
          padding: cur.padding != null ? cur.padding : 30,
        }),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.detail || r.statusText);
      }
      /* Mutate the manifest in place so a re-visit / Reset would restore to
         the new box, and so the QC thumbnail picks up the new image on its
         next render (it loads via /api/file which is cache-busted). */
      setManifest((prev) => ({
        ...prev,
        crops: { ...prev.crops,
                 [curPath]: { ...prev.crops[curPath], ...draftBox } },
      }));
      setDraftBox(null);
      setFlash("saved");
      setTimeout(() => setFlash(null), 1400);
      /* Auto-advance is a real volunteer accelerator — they almost always
         redraw a series of crops in a row. Skip the bump on the last crop. */
      if (idx < cropPaths.length - 1) {
        setIdx(idx + 1);
      }
    } catch (e) {
      setFlash("failed:" + e.message);
      setTimeout(() => setFlash(null), 3200);
    } finally {
      setSaving(false);
    }
  }

  /* ── Keyboard shortcuts ────────────────────────────────────────────── */
  useEffectS3(() => {
    const onKey = (e) => {
      if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA")) return;
      if (e.key === "Enter")       { if (draftBox) accept(); }
      else if (e.key === "r" || e.key === "R") setDraftBox(null);
      else if (e.key === "ArrowLeft")  goto(idx - 1);
      else if (e.key === "ArrowRight") goto(idx + 1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

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

  // Bbox placement — uses the effective box (draft || manifest) so it tracks
  // live while the user drags.
  const boxStyle = view && effective ? {
    position: "absolute",
    left:   (effective.x - view.vx) * view.scale,
    top:    (effective.y - view.vy) * view.scale,
    width:  effective.w * view.scale,
    height: effective.h * view.scale,
    cursor: saving ? "wait" : "move",
  } : { display: "none" };

  return (
    <div className="redraw">
      <div className="redraw-main">

        {/* Editor canvas */}
        <div className="editor-wrap grow" style={{ minWidth: 0 }}>
          <div ref={canvasRef} className="editor-canvas"
               onMouseDown={(e) => { if (e.target === e.currentTarget || e.target.tagName === "IMG") startPan(e); }}
               onWheel={onWheel}
               style={{ position: "relative", width: "100%", height: "100%",
                        overflow: "hidden",
                        cursor: panRef.current ? "grabbing" : "grab" }}>
            {view && <>
              <img src={"/api/file?path=" + encodeURIComponent(cur.source_path)}
                   alt="" style={imgStyle} draggable={false} />
              <div className="bbox" style={boxStyle}
                   onMouseDown={(e) => startDrag(e, "move")}>
                {["nw","n","ne","e","se","s","sw","w"].map(
                  (h) => <Handle key={h} pos={h} onDrag={startDrag} />)}
                <span className="bbox-dim">
                  {effective.w} × {effective.h} px{draftBox ? "  ·  unsaved" : ""}
                </span>
              </div>
            </>}
            <span className="ph-label" style={{ position: "absolute", bottom: 12, left: 14,
                                                 textAlign: "left",
                                                 color: "var(--text-3)",
                                                 fontFamily: "var(--mono)",
                                                 fontSize: 10,
                                                 pointerEvents: "none" }}>
              packet {String(cur ? cur.packet_index : 0).padStart(2, "0")} · drag to pan · scroll to zoom{zoom > 1 ? `  (${zoom.toFixed(1)}×)` : ""}
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
                  flagged={miniFlags}
                  tag={cur ? cur.source_path.split(/[\\/]/).pop() : ""} />
              </div>
              <div className="hint" style={{ marginTop: 9 }}>
                Editing crop <b style={{ color: "var(--text)" }}>
                  {String(cur ? cur.packet_index : 0).padStart(2, "0")}
                </b> · cell colours reflect QC status (green = approved, red = flagged).
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
                <span className="mv">{effective ? effective.x : "—"} · {effective ? effective.y : "—"}</span>
                <span className="mk">W · H</span>
                <span className="mv">{effective ? effective.w : "—"} · {effective ? effective.h : "—"}</span>
                <span className="mk">Padding</span>
                <span className="mv">{cur && cur.padding != null ? cur.padding + " px" : "(deskewed → 30)"}</span>
              </div>
            </div>
          </div>

          <div className="redraw-actions">
            <button className="btn btn-primary btn-lg" style={{ flex: 1 }}
              disabled={!draftBox || saving}
              onClick={accept}>
              <Icon d={ICONS.check} size={15} /> {saving ? "Saving…" : "Accept Boundary"}
            </button>
            <button className="btn btn-lg" title="Reset to detected box"
              disabled={!draftBox || saving}
              onClick={() => setDraftBox(null)}>
              <Icon paths={ICONS.reset} size={15} />
            </button>
          </div>
          {flash &&
            <div className="hint" style={{
              textAlign: "center",
              color: flash === "saved" ? "var(--green)" : "var(--red-bright)",
              fontWeight: 600,
            }}>
              {flash === "saved" ? "✓  Crop saved" : "✗  " + flash.replace(/^failed:/, "")}
            </div>
          }
          {!flash &&
            <div className="hint" style={{ textAlign: "center" }}>
              <span className="kbd">Enter</span> accept ·{" "}
              <span className="kbd">R</span> reset ·{" "}
              <span className="kbd">←/→</span> prev / next
            </div>
          }

        </div>
      </div>
    </div>
  );
}

Object.assign(window, { RedrawBoundary });

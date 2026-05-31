/* global React, Region, AnnoNote, Icon, ICONS, Pill, Placeholder */
const { useState: useStateS2, useEffect: useEffectS2, useMemo: useMemoS2 } = React;

/* ── Persistent QC state ─────────────────────────────────────────────────
   Per-crop user overrides (approve / flag + notes) live in a window-scope
   bag keyed by crop path, in the same spirit as window.__CDZ_SETUP_STATE.
   Switching tabs preserves the user's review decisions. */
if (!window.__CDZ_QC_STATE) window.__CDZ_QC_STATE = { overrides: {}, notes: {}, selectedPath: null };
const QC = window.__CDZ_QC_STATE;

/* ── Filters ─────────────────────────────────────────────────────────────
   Counts are computed live from the current crop list. */
const FILTERS = [
  { k: "all",        label: "All" },
  { k: "approved",   label: "Approved" },
  { k: "unreviewed", label: "Unreviewed" },
  { k: "flagged",    label: "Flagged" },
  { k: "oversize",   label: "Oversize" },
];

const STATUS_RING = {
  flagged:    "red",
  oversize:   "gold",
  approved:   "green",
  unreviewed: "neutral",
};

/* ── Card — replaces the synthetic Placeholder with the real packet image.
   We keep the exact wrapper structure (.thumb / .thumb-ph / .thumb-foot)
   so every skin's CSS overrides keep applying without any changes. */
function CropThumb({ c, selected, multi, onClick }) {
  const ring = STATUS_RING[c.status] || "neutral";
  const cls  = ["thumb", ring,
                selected ? "sel"   : "",
                multi    ? "multi" : ""].filter(Boolean).join(" ");
  return (
    <button className={cls}
      onClick={(e) => onClick(c, e)}
      onContextMenu={(e) => e.preventDefault()}>
      <div className="thumb-ph">
        {/* output crops have no EXIF (cv2.imwrite writes raw pixels), so
            oriented=0 is the right + cheaper path for thumbnails */}
        <img className="thumb-img" loading="lazy"
          src={"/api/file?oriented=0&path=" + encodeURIComponent(c.path)}
          alt={c.name} />
        <span className="thumb-num">{c.idxLabel}</span>
      </div>
      <div className="thumb-foot">
        <span className="mono"
          style={{ fontSize: 9.5, color: "var(--text-3)", overflow: "hidden",
                   textOverflow: "ellipsis", whiteSpace: "nowrap" }}
          title={c.name}>{c.stem}</span>
        <span className={`sdot ${ring}`} />
      </div>
    </button>);
}

/* Empty state shown when no batch has been run yet (no __CDZ_SESSION). */
function QCEmpty() {
  return (
    <div className="qc-empty">
      <div className="qc-empty-card">
        <Icon paths={ICONS.warn} size={28} />
        <div className="qc-empty-title">No batch loaded</div>
        <div className="qc-empty-sub">
          Run a segmentation on the <b>Session Setup</b> tab to populate the QC review.
        </div>
      </div>
    </div>);
}

function QCReview() {
  const session = window.__CDZ_SESSION || null;

  const [crops,     setCrops]     = useStateS2([]);
  const [loading,   setLoading]   = useStateS2(false);
  const [err,       setErr]       = useStateS2(null);
  const [overrides, setOverrides] = useStateS2(() => ({ ...QC.overrides }));
  const [notes,     setNotes]     = useStateS2(() => ({ ...QC.notes }));
  const [filter,    setFilter]    = useStateS2("all");
  const [selPath,   setSelPath]   = useStateS2(QC.selectedPath);
  const [multiSel,  setMultiSel]  = useStateS2(() => new Set(QC.multi || []));

  /* Load real crops on mount / when the batch output dir changes. */
  useEffectS2(() => {
    if (!session || !session.outputDir) return;
    setLoading(true); setErr(null);
    fetch("/api/crops?output_dir=" + encodeURIComponent(session.outputDir))
      .then((r) => r.ok ? r.json() : r.json().then((j) => Promise.reject(j.detail || r.statusText)))
      .then((j) => {
        /* Build a fast lookup of source-image flags → derives per-crop default
           status. A crop inherits "oversize" if its source image was flagged
           oversize; the per-crop precise oversize set lives in
           flagged[].crop_info so we use that for an exact match. */
        const oversizePaths = new Set();
        (session.flagged || []).forEach((r) => {
          if (r.flag === "oversize") {
            (r.crop_info || []).forEach((ci) => oversizePaths.add(ci.path));
          }
        });
        const built = (j.crops || []).map((c, i) => {
          const parts  = c.stem.match(/^(.*)_packet_(\d+)$/);
          const src    = parts ? parts[1] : c.stem;
          const packet = parts ? parseInt(parts[2], 10) : (i + 1);
          const isOver = oversizePaths.has(c.path);
          return {
            path: c.path,
            name: c.name,
            stem: c.stem,
            src,
            packet,
            idxLabel: String(packet).padStart(2, "0"),
            sizeBytes: c.size_bytes,
            status: isOver ? "oversize" : "unreviewed",
          };
        });
        setCrops(built);
      })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, [session && session.outputDir]);

  /* Apply user overrides on top of the derived status. */
  const cropsWithStatus = useMemoS2(() =>
    crops.map((c) => ({ ...c, status: overrides[c.path] || c.status })),
    [crops, overrides]);

  /* Live filter counts. */
  const counts = useMemoS2(() => {
    const out = { all: cropsWithStatus.length };
    for (const c of cropsWithStatus) out[c.status] = (out[c.status] || 0) + 1;
    return out;
  }, [cropsWithStatus]);

  const visible = useMemoS2(
    () => filter === "all" ? cropsWithStatus : cropsWithStatus.filter((c) => c.status === filter),
    [cropsWithStatus, filter]);

  /* Selection: default to first visible crop when nothing or stale. */
  const cur = useMemoS2(() => {
    if (selPath) {
      const hit = cropsWithStatus.find((c) => c.path === selPath);
      if (hit) return hit;
    }
    return visible[0] || cropsWithStatus[0] || null;
  }, [cropsWithStatus, visible, selPath]);

  /* Mirror user state into the persistent bag. */
  function setStatus(path, newStatus) {
    setOverrides((prev) => {
      const next = { ...prev };
      if (!newStatus || newStatus === "unreviewed") delete next[path];
      else next[path] = newStatus;
      QC.overrides = next;
      return next;
    });
  }
  function setNote(path, note) {
    setNotes((prev) => {
      const next = { ...prev };
      if (note) next[path] = note; else delete next[path];
      QC.notes = next;
      return next;
    });
  }
  function selectCrop(c, ev) {
    /* Shift-click extends a contiguous range from the previous anchor; plain
       click selects a single crop and resets the anchor. */
    if (ev && ev.shiftKey && selPath) {
      const list = visible;
      const a = list.findIndex((x) => x.path === selPath);
      const b = list.findIndex((x) => x.path === c.path);
      if (a >= 0 && b >= 0) {
        const lo = Math.min(a, b), hi = Math.max(a, b);
        const range = list.slice(lo, hi + 1).map((x) => x.path);
        QC.multi = new Set(range);
        setMultiSel(new Set(range));
      }
    } else {
      QC.multi = new Set();
      setMultiSel(new Set());
    }
    setSelPath(c.path);
    QC.selectedPath = c.path;
  }

  function bulkApprove() {
    /* When the user has a shift-selected range, approve those specifically;
       otherwise approve every currently-visible unreviewed crop. Functional
       update so we don't lose concurrent setStatus writes. */
    setOverrides((prev) => {
      const next = { ...prev };
      if (multiSel.size > 0) {
        for (const p of multiSel) next[p] = "approved";
      } else {
        for (const c of visible) {
          if (c.status === "unreviewed") next[c.path] = "approved";
        }
      }
      QC.overrides = next;
      return next;
    });
  }

  function openRedrawFor(c) {
    /* Hand off the selected crop to the Redraw screen.  Both screens share
       the same manifest, sorted lexicographically; we pre-set RS.idx so the
       Redraw view opens on this crop, then trigger a tab switch via the
       App's setTab — exposed on window for cross-screen navigation. */
    const allPaths = Object.keys(window.__CDZ_REDRAW_MANIFEST_PATHS || {})
      .concat([c.path]); // fallback if redraw hasn't mounted yet
    /* RS lives in RedrawBoundary.jsx; index is the sorted position of the
       crop path within the manifest. We compute by sorting the QC crops in
       the same order Redraw does. */
    const sorted = [...crops].sort((a, b) => a.path.localeCompare(b.path));
    const i = sorted.findIndex((x) => x.path === c.path);
    if (!window.__CDZ_REDRAW_STATE) window.__CDZ_REDRAW_STATE = { idx: 0 };
    window.__CDZ_REDRAW_STATE.idx = i >= 0 ? i : 0;
    if (typeof window.__CDZ_SET_TAB === "function") {
      window.__CDZ_SET_TAB("redraw");
    }
  }

  /* ── Empty state — no batch in this session ──────────────────────── */
  if (!session) return <QCEmpty />;

  return (
    <div className="qc">
      {/* Toolbar — single row: bulk actions | filter chips (flex:1) | count + finalise */}
      <div className="qc-toolbar" style={{ flexWrap: "nowrap", gap: 10 }}>

        {/* Bulk actions */}
        <div className="row" style={{ gap: 8, flexShrink: 0 }}>
          <button className="btn btn-sm" onClick={bulkApprove}>
            <Icon d={ICONS.check} size={14} />{" "}
            {multiSel.size > 0
              ? `Approve ${multiSel.size} selected`
              : "Approve all visible"}
          </button>
          <button className="btn btn-sm"
            disabled={!cur && multiSel.size === 0}
            onClick={() => {
              setOverrides((prev) => {
                const next = { ...prev };
                const targets = multiSel.size > 0 ? [...multiSel] : (cur ? [cur.path] : []);
                for (const p of targets) next[p] = "flagged";
                QC.overrides = next;
                return next;
              });
            }}>
            <Icon paths={ICONS.flag} size={14} />{" "}
            {multiSel.size > 0
              ? `Flag ${multiSel.size} selected`
              : "Flag selected"}
          </button>
        </div>

        <div className="divider-v" />

        {/* Filter chips — expands to fill available space */}
        <div className="filterbar" style={{ flexWrap: "nowrap", flex: 1, minWidth: 0 }}>
          {FILTERS.map((f) => {
            const n = counts[f.k] || 0;
            return (
              <button key={f.k} className={`fchip ${filter === f.k ? "on" : ""}`}
                onClick={() => setFilter(f.k)}>
                {f.label}<span className="fn">{n}</span>
              </button>);
          })}
        </div>

        <div className="divider-v" />

        {/* Count + finalise */}
        <div className="row" style={{ gap: 8, flexShrink: 0, alignItems: "center" }}>
          <span className="hint" style={{ whiteSpace: "nowrap" }}>
            <b style={{ color: "var(--green)" }}>{counts.approved || 0}</b> of {counts.all} approved
          </span>
          <div className="row" style={{ gap: 8 }}>
            <button className="btn btn-sm">
              <Icon d={ICONS.download} size={14} /> Export Crops
            </button>
            <button className="btn btn-sm btn-primary">
              <Icon d={ICONS.send} size={14} /> Proceed to Submission
            </button>
          </div>
        </div>

      </div>

      <div className="qc-body">
        {/* Thumbnail grid */}
        <div className="qc-grid-wrap grow">
          {loading && <div className="qc-msg">Loading crops…</div>}
          {err && <div className="qc-msg err">⚠ {err}</div>}
          {!loading && !err && visible.length === 0 &&
            <div className="qc-msg">No crops match this filter.</div>
          }
          {!loading && !err && visible.length > 0 &&
            <div className="qc-grid">
              {visible.map((c) =>
                <CropThumb key={c.path} c={c}
                  selected={cur && cur.path === c.path}
                  multi={multiSel.has(c.path)}
                  onClick={selectCrop} />
              )}
            </div>
          }
        </div>

        {/* Detail panel */}
        <div className="qc-detail">
          <div className="panel" style={{ height: "100%", display: "flex", flexDirection: "column" }}>
            <div className="panel-head">
              <span className="panel-title">Crop Detail</span>
              {cur && <Pill status={cur.status} />}
            </div>
            <div className="panel-body col" style={{ gap: 14, overflowY: "auto" }}>
              {!cur && <div className="hint">No crop selected.</div>}
              {cur && <>
                <div className="detail-img qc-detail-img">
                  <img src={"/api/file?path=" + encodeURIComponent(cur.path)} alt={cur.name} />
                </div>

                {cur.status === "flagged" &&
                  <div className="callout red col" style={{ gap: 8 }}>
                    <span className="row" style={{ gap: 8 }}>
                      <Icon paths={ICONS.warn} size={14} style={{ flexShrink: 0 }} /> Manually flagged
                    </span>
                    <textarea className="input flag-reason"
                      value={notes[cur.path] || ""}
                      placeholder="Describe the issue…"
                      onChange={(e) => setNote(cur.path, e.target.value)}
                      rows={2} />
                  </div>
                }
                {cur.status === "oversize" &&
                  <div className="callout gold">
                    <Icon paths={ICONS.warn} size={14} />
                    <span>Source image was flagged: likely two merged packets in this crop.</span>
                  </div>
                }

                <div className="meta-grid">
                  <span className="mk">Source image</span><span className="mv">{cur.src}</span>
                  <span className="mk">Packet index</span><span className="mv">{cur.idxLabel}</span>
                  <span className="mk">File</span>
                  <span className="mv" title={cur.name}
                    style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{cur.name}</span>
                  <span className="mk">Size</span>
                  <span className="mv">{Math.round((cur.sizeBytes || 0) / 1024)} KB</span>
                </div>

                <div className="detail-actions">
                  <button className="btn btn-primary" style={{ flex: 1 }}
                    onClick={() => setStatus(cur.path, "approved")}>
                    <Icon d={ICONS.check} size={14} /> Approve
                  </button>
                  <button className="btn"
                    onClick={() => setStatus(cur.path, "flagged")}>
                    <Icon paths={ICONS.flag} size={14} /> Flag
                  </button>
                  <button className="btn"
                    onClick={() => openRedrawFor(cur)}>
                    <Icon paths={ICONS.crop} size={14} /> Redraw
                  </button>
                </div>
                <div className="hint" style={{ marginTop: -2 }}>
                  <span className="kbd">A</span> approve · <span className="kbd">F</span> flag · <span className="kbd">R</span> redraw boundary · <span className="kbd">←/→</span> navigate
                </div>
              </>}
            </div>
          </div>
        </div>
      </div>
    </div>);
}

Object.assign(window, { QCReview });

/* global React, Region, AnnoNote, Icon, ICONS, Pill, Placeholder */
const { useState: useStateS2 } = React;

/* Build a deterministic set of sample crops */
const QC_CROPS = (() => {
  const arr = [];
  const statuses = [
  "approved", "approved", "approved", "unreviewed", "oversize", "approved",
  "approved", "flagged", "approved", "unreviewed", "approved", "approved",
  "approved", "approved", "none", "approved", "unreviewed", "approved",
  "approved", "oversize", "approved", "approved", "approved", "unreviewed"];

  const imgs = ["IMG_0241", "IMG_0242", "IMG_0243"];
  for (let i = 0; i < statuses.length; i++) {
    arr.push({
      id: i + 1,
      status: statuses[i],
      src: imgs[Math.floor(i / 8)],
      idx: i % 8 + 1,
      w: statuses[i] === "oversize" ? 3104 : 1180 + i % 5 * 40,
      h: statuses[i] === "oversize" ? 1870 : 1520 + i % 4 * 30
    });
  }
  return arr;
})();

const FILTERS = [
{ k: "all", label: "All", n: 24 },
{ k: "approved", label: "Approved", n: 15 },
{ k: "unreviewed", label: "Unreviewed", n: 4 },
{ k: "flagged", label: "Flagged", n: 1 },
{ k: "oversize", label: "Oversize", n: 2 },
{ k: "none", label: "No pkts", n: 1 }];


function CropThumb({ c, selected, onClick }) {
  const ring = c.status === "flagged" || c.status === "none" ? "red" :
  c.status === "oversize" ? "gold" :
  c.status === "approved" ? "green" : "neutral";
  return (
    <button className={`thumb ${ring} ${selected ? "sel" : ""}`} onClick={onClick}>
      <Placeholder className="thumb-ph" tag={`${String(c.idx).padStart(2, "0")}`}>
        <span className="ph-label" style={{ fontSize: 9 }}>packet crop</span>
      </Placeholder>
      <div className="thumb-foot">
        <span className="mono" style={{ fontSize: 9.5, color: "var(--text-3)" }}>{c.src}_{String(c.idx).padStart(2, "0")}</span>
        <span className={`sdot ${ring}`} />
      </div>
    </button>);

}

function QCReview() {
  const [sel, setSel] = useStateS2(7); // a flagged one
  const [filter, setFilter] = useStateS2("all");
  const cur = QC_CROPS[sel];

  return (
    <div className="qc">
      {/* Toolbar — single row: bulk actions | filter chips (flex:1) | count + finalise */}
      <Region n={1} label="QC toolbar — bulk actions, filter, proceed" pos="tl"
        className="qc-toolbar" style={{ flexWrap: "nowrap", gap: 10 }}>

        {/* Bulk actions */}
        <div className="row" style={{ gap: 8, flexShrink: 0 }}>
          <Region n={2} label="bulk approve" pos="bl">
            <button className="btn btn-sm"><Icon d={ICONS.check} size={14} /> Approve all visible</button>
          </Region>
          <button className="btn btn-sm"><Icon paths={ICONS.flag} size={14} /> Flag selected</button>
        </div>

        <div className="divider-v" />

        {/* Filter chips — expands to fill available space */}
        <Region n={3} label="filter by status" pos="bl" style={{ flex: 1, minWidth: 0 }}>
          <div className="filterbar" style={{ flexWrap: "nowrap" }}>
            {FILTERS.map((f) =>
              <button key={f.k} className={`fchip ${filter === f.k ? "on" : ""}`} onClick={() => setFilter(f.k)}>
                {f.label}<span className="fn">{f.n}</span>
              </button>
            )}
          </div>
        </Region>

        <div className="divider-v" />

        {/* Count + finalise */}
        <div className="row" style={{ gap: 8, flexShrink: 0, alignItems: "center" }}>
          <span className="hint" style={{ whiteSpace: "nowrap" }}>
            <b style={{ color: "var(--green)" }}>15</b> of 24 approved
          </span>
          {/* Two finalise paths: standalone export (no VVGo) or full API submission */}
          <Region n={7} label="finalise — export crops or proceed to VVGo submission" pos="bl">
            <div className="row" style={{ gap: 8 }}>
              <button className="btn btn-sm">
                <Icon d={ICONS.download} size={14} /> Export Crops
              </button>
              <button className="btn btn-sm btn-primary" data-comment-anchor="af82a3ca25-button-81-13">
                <Icon d={ICONS.send} size={14} /> Proceed to Submission
              </button>
            </div>
          </Region>
        </div>

      </Region>

      <div className="qc-body">
        {/* Thumbnail grid */}
        <Region n={4} label="segmented crop browser — click to select" pos="tr" className="qc-grid-wrap grow">
          <div className="qc-grid">
            {QC_CROPS.map((c, i) =>
            <CropThumb key={c.id} c={c} selected={i === sel} onClick={() => setSel(i)} />
            )}
          </div>
        </Region>

        {/* Detail panel */}
        <Region n={5} label="detail panel — selected crop" pos="tl" className="qc-detail">
          <div className="panel" style={{ height: "100%", display: "flex", flexDirection: "column" }}>
            <div className="panel-head">
              <span className="panel-title">Crop Detail</span>
              <Pill status={cur.status} />
            </div>
            <div className="panel-body col" style={{ gap: 14, overflowY: "auto" }}>
              <Placeholder className="detail-img" tag={`${cur.src}_packet_${String(cur.idx).padStart(2, "0")}`}
              style={{ height: 230 }} label="[ enlarged packet crop ]" />

              {cur.status === "flagged" &&
              <div className="callout red col" data-comment-anchor="6cf4f03c9d-div-108-17" style={{ gap: 8 }}>
                  <span className="row" style={{ gap: 8 }}>
                    <Icon paths={ICONS.warn} size={14} style={{ flexShrink: 0 }} /> Manually flagged
                  </span>
                  <textarea className="input flag-reason"
                    defaultValue="Label text partly cut off at right edge."
                    placeholder="Describe the issue…"
                    rows={2} />
                </div>
              }
              {cur.status === "oversize" &&
              <div className="callout gold">
                  <Icon paths={ICONS.warn} size={14} />
                  <span>Oversize crop ({cur.w} × {cur.h} px vs median 1180 × 1520). Likely two merged packets.</span>
                </div>
              }

              <div className="meta-grid">
                <span className="mk">Source image</span><span className="mv">{cur.src}.tif</span>
                <span className="mk">Packet index</span><span className="mv">{String(cur.idx).padStart(2, "0")} of 12</span>
                <span className="mk">Dimensions</span><span className="mv">{cur.w} × {cur.h} px</span>
                <span className="mk">Detected fg</span><span className="mv">light</span>
                <span className="mk">Rectangularity</span><span className="mv">0.84</span>
              </div>

              <Region n={6} label="per-crop actions" pos="tl">
                <div className="detail-actions">
                  <button className="btn btn-primary" style={{ flex: 1 }}><Icon d={ICONS.check} size={14} /> Approve</button>
                  <button className="btn"><Icon paths={ICONS.flag} size={14} /> Flag</button>
                  <button className="btn"><Icon paths={ICONS.crop} size={14} /> Redraw</button>
                </div>
              </Region>
              <div className="hint" style={{ marginTop: -2 }}>
                <span className="kbd">A</span> approve · <span className="kbd">F</span> flag · <span className="kbd">R</span> redraw boundary · <span className="kbd">←/→</span> navigate
              </div>
            </div>
          </div>
        </Region>
      </div>
    </div>);

}

Object.assign(window, { QCReview });
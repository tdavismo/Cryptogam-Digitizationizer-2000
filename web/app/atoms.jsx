/* global React */
const { createContext, useContext, useState } = React;

/* ── Annotation context ──────────────────────────────────────── */
const AnnoCtx = createContext(true);

/* Region: wraps a UI region with a dashed outline + floating mono label.
   `n` is the callout number, `label` the text, `pos` corner placement. */
function Region({ n, label, pos = "tl", as = "div", className = "", style, children, ...rest }) {
  const Tag = as;
  return (
    <Tag className={`region ${className}`} style={style} {...rest}>
      {label &&
      <span className={`anno-label ${pos}`}>
          {n != null && <span className="num">{n}</span>}
          {label}
        </span>
      }
      {children}
    </Tag>);

}

/* Free-floating annotation note positioned absolutely inside a relative parent */
function AnnoNote({ style, children }) {
  return <div className="anno-note" style={style}>{children}</div>;
}

/* ── Fern mark (reused from the herbarium aesthetic baseline) ──── */
function FernMark({ className = "fern", color = "var(--green)" }) {
  return (
    <svg className={className} viewBox="0 0 48 64" fill="none" xmlns="http://www.w3.org/2000/svg">
      <g stroke={color} strokeWidth="1.4" strokeLinecap="round" fill="none">
        <path d="M24 60 Q25 40 27 20 Q28 10 29 2" />
        <path d="M27 20 Q35 16 42 18" /><path d="M27 20 Q19 16 12 18" />
        <path d="M27.5 15 Q35 10 40 12" /><path d="M27.5 15 Q20 10 15 12" />
        <path d="M28 10 Q33 6 37 7.5" /><path d="M28 10 Q23 6 19 7.5" />
        <path d="M26.5 26 Q35 22 44 24" /><path d="M26.5 26 Q18 22 9 24" />
        <path d="M26 32 Q34 27 44 30" /><path d="M26 32 Q18 27 8 30" />
        <path d="M25.5 39 Q34 34 45 37" /><path d="M25.5 39 Q17 34 6 37" />
        <path d="M25 46 Q33 40 44 44" /><path d="M25 46 Q17 40 6 44" />
        <path d="M24.5 53 Q32 48 42 51" /><path d="M24.5 53 Q17 48 7 51" />
      </g>
    </svg>);

}

/* ── Icons (simple line icons, 24x24 stroke) ──────────────────── */
function Icon({ d, paths, size = 16, className = "ico", fill = "none", style }) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill={fill}
    stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" style={style}>
      {d && <path d={d} />}
      {paths}
    </svg>);

}
const ICONS = {
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  key: <g key="k"><circle cx="8" cy="12" r="4" /><path d="M12 12h9M18 12v3M21 12v2" /></g>,
  grid: <g key="g"><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></g>,
  play: "M7 5l12 7-12 7z",
  check: "M5 13l4 4L19 7",
  flag: <g key="f"><path d="M5 21V4M5 4h12l-2 4 2 4H5" /></g>,
  filter: "M3 5h18l-7 8v6l-4-2v-4z",
  arrowLeft: "M15 6l-6 6 6 6",
  arrowRight: "M9 6l6 6-6 6",
  reset: <g key="r"><path d="M3 12a9 9 0 1 0 3-6.7M3 4v4h4" /></g>,
  send: "M4 12l16-7-7 16-2-7z",
  retry: <g key="rt"><path d="M3 12a9 9 0 1 0 3-6.7M3 4v4h4" /></g>,
  x: "M6 6l12 12M18 6L6 18",
  eye: <g key="e"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="2.5" /></g>,
  warn: <g key="w"><path d="M12 3l9 16H3z" /><path d="M12 10v4M12 17v.5" /></g>,
  doc: <g key="d"><path d="M6 2h8l4 4v16H6z" /><path d="M14 2v4h4M9 13h6M9 17h6" /></g>,
  crop: <g key="c"><path d="M6 2v16h16M2 6h16v16" /></g>,
  layers: <g key="l"><path d="M12 3l9 5-9 5-9-5z" /><path d="M3 13l9 5 9-5" /></g>,
  zoom: <g key="z"><circle cx="11" cy="11" r="7" /><path d="M21 21l-4-4M11 8v6M8 11h6" /></g>,
  download: "M12 16V4M8 12l4 4 4-4M4 20h16"
};

/* ── Status pill ─────────────────────────────────────────────── */
const STATUS = {
  unreviewed: { cls: "neutral", text: "Unreviewed" },
  approved: { cls: "green", text: "Approved" },
  flagged: { cls: "red", text: "Flagged" },
  oversize: { cls: "gold", text: "Oversize" },
  none: { cls: "red", text: "No packets" },
  queued: { cls: "neutral", text: "Queued" },
  submitted: { cls: "blue", text: "Submitting", pulse: true },
  complete: { cls: "green", text: "Complete" },
  error: { cls: "red", text: "Error" },
  /* VVGo audit statuses (json-on-disk = received is authoritative) */
  received: { cls: "green", text: "Received" },
  needs_output: { cls: "gold", text: "Needs output" },
  errored: { cls: "red", text: "Errored" },
  not_submitted: { cls: "neutral", text: "Not submitted" }
};
function Pill({ status, children }) {
  const s = STATUS[status] || { cls: "neutral", text: status };
  return (
    <span className={`pill ${s.cls}${s.pulse ? " pulse" : ""}`}>
      <span className="pdot" />
      {children || s.text}
    </span>);

}

/* ── Striped placeholder ─────────────────────────────────────── */
function Placeholder({ label, tag, style, className = "", children }) {
  return (
    <div className={`ph ${className}`} style={style}>
      {tag && <span className="ph-tag">{tag}</span>}
      {label && <span className="ph-label">{label}</span>}
      {children}
    </div>);

}

/* ── Scan preview (shared by Session Setup + Redraw Boundary) ── */

/* Seeded deterministic pseudo-random — stable across renders */
function seededRand(seed) {
  const x = Math.sin(seed * 127.1 + 311.7) * 43758.5453;
  return x - Math.floor(x);
}
function seededJitter(seed, scale) { return (seededRand(seed) - 0.5) * scale; }

/* Rotate a rectangle's four corners around its centre */
function rotatedRectPts(cx, cy, w, h, angleDeg) {
  const a = (angleDeg * Math.PI) / 180;
  const cos = Math.cos(a), sin = Math.sin(a);
  return [[-w/2,-h/2],[w/2,-h/2],[w/2,h/2],[-w/2,h/2]].map(
    ([x,y]) => [cx + x*cos - y*sin, cy + x*sin + y*cos]
  );
}

/*
 * ScanPreview — opaque photo stand-in + non-orthometric SVG bounding-box overlay.
 *
 * Props:
 *   rows, cols       grid dimensions (drive reading-order numbering)
 *   flagged          { cellIndex: "gold"|"red" } — status colour overrides
 *   activeIndex      cell to highlight as currently-editing (Redraw Boundary minimap)
 *   tag              top-left filename badge (optional)
 *   label            bottom-right descriptor text (optional)
 *   commentAnchor    data-comment-anchor value forwarded to root div (optional)
 */
function ScanPreview({ rows=4, cols=3, flagged={}, activeIndex=null, tag=null, label=null, commentAnchor=null }) {
  const VW = 600, VH = 420, mx = 28, my = 22;
  const cellW = (VW - mx*2) / cols;
  const cellH = (VH - my*2) / rows;
  const inset = 10;

  const rootProps = commentAnchor ? { "data-comment-anchor": commentAnchor } : {};

  return (
    <div {...rootProps} style={{ position:"relative", width:"100%", height:"100%" }}>
      <div className="scan-photo">
        {tag && <span className="ph-tag">{tag}</span>}
        {label && <span className="scan-label" style={{ whiteSpace:"pre-line" }}>{label}</span>}
      </div>
      <svg viewBox={`0 0 ${VW} ${VH}`} preserveAspectRatio="xMidYMid meet"
        style={{ position:"absolute", inset:0, width:"100%", height:"100%", pointerEvents:"none" }}>
        {Array.from({ length: rows * cols }).map((_, i) => {
          const row = Math.floor(i / cols), col = i % cols;
          const isActive = activeIndex !== null && i === activeIndex;
          const flag = flagged[i];

          const cx = mx + col*cellW + cellW/2;
          const cy = my + row*cellH + cellH/2;
          const bw = cellW - inset*2, bh = cellH - inset*2;

          const angleDeg = seededJitter(i*5+1, 6);
          const rotated  = rotatedRectPts(cx, cy, bw, bh, angleDeg);
          const J = 3;
          const pts = rotated
            .map(([x,y],k) => [x + seededJitter(i*8+k*2,J), y + seededJitter(i*8+k*2+1,J)])
            .map(p => p.join(",")).join(" ");

          const stateClass = isActive ? "bb-active"
            : flag === "red"   ? "bb-red"
            : flag === "gold"  ? "bb-gold"
            : flag === "green" ? "bb-green"
            : "bb-ok";

          const num = String(i+1).padStart(2,"0");
          const lx = mx + col*cellW + inset + 3;
          const ly = my + row*cellH + inset + 11;

          return (
            <g key={i} className={`bb ${stateClass}`}>
              <polygon points={pts} strokeLinejoin="round" />
              <text x={lx} y={ly} fontSize="9.5"
                fontFamily="var(--bb-font, 'IBM Plex Mono', monospace)" fontWeight="600" opacity="0.9">
                {num}{isActive ? " ◀" : ""}
              </text>
            </g>);
        })}
      </svg>
    </div>);
}

/* ── Field wrapper ───────────────────────────────────────────── */
function Field({ label, req, children, hint }) {
  return (
    <div className="field">
      {label && <label className="label">{label}{req && <span className="req">*</span>}</label>}
      {children}
      {hint && <div className="hint" style={{ marginTop: 6 }}>{hint}</div>}
    </div>);

}

Object.assign(window, {
  AnnoCtx, Region, AnnoNote, FernMark, Icon, ICONS, Pill, STATUS, Placeholder, Field,
  seededRand, seededJitter, rotatedRectPts, ScanPreview
});
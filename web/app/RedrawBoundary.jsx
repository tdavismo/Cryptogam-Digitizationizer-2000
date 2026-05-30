/* global React, Region, AnnoNote, Icon, ICONS, Placeholder, ScanPreview */

function Handle({ pos }) {
  return <span className={`bbox-handle ${pos}`} />;
}

function RedrawBoundary() {
  const handles = ["nw","n","ne","e","se","s","sw","w"];
  return (
    <div className="redraw">
      <div className="redraw-main">
        {/* Full-size editor */}
        <Region n={1} label="manual crop editor — drag handles to resize / reposition" pos="tr" className="editor-wrap grow">
          <Placeholder className="editor-canvas" tag="IMG_0242.tif · region around packet 05"
            style={{ width: "100%", height: "100%" }}>
            <span className="ph-label" style={{ position: "absolute", bottom: 12, left: 14, textAlign: "left" }}>
              [ auto-zoomed to packet 05 · full-resolution source region ]<br />two packets merged into one detection
            </span>
            {/* editable bounding box */}
            <div className="bbox">
              {handles.map((h) => <Handle key={h} pos={h} />)}
              <span className="bbox-dim">1180 × 1524 px</span>
            </div>
            <AnnoNote style={{ top: 18, right: 18 }}>2 · 8 drag handles — corners scale, edges stretch</AnnoNote>
            <AnnoNote style={{ bottom: 18, left: 14 }}>1 · editor auto-zooms to the selected packet — minimap shows its position in the full scan</AnnoNote>
          </Placeholder>
        </Region>

        {/* Right rail: context minimap + nav + actions */}
        <div className="redraw-rail">
          <Region n={3} label="context — position in full scan" pos="tl">
            <div className="panel">
              <div className="panel-head"><span className="panel-title">Source Context</span></div>
              <div className="panel-body">
                <div style={{ height: 168, position: "relative" }}>
                  <ScanPreview rows={4} cols={3} activeIndex={4} tag="IMG_0242.tif" />
                </div>
                <div className="hint" style={{ marginTop: 9 }}>
                  Editing crop <b style={{ color: "var(--text)" }}>05</b> · highlighted box shows the current
                  region within the full source scan.
                </div>
              </div>
            </div>
          </Region>

          <Region n={4} label="crop navigation" pos="tl">
            <div className="panel">
              <div className="panel-body row between">
                <button className="btn btn-sm"><Icon d={ICONS.arrowLeft} size={15} /> Prev</button>
                <span className="mono" style={{ fontSize: 12, color: "var(--text-2)" }}>crop 05 / 12</span>
                <button className="btn btn-sm">Next <Icon d={ICONS.arrowRight} size={15} /></button>
              </div>
            </div>
          </Region>

          <div className="redraw-coords panel">
            <div className="panel-body">
              <div className="meta-grid">
                <span className="mk">X · Y</span><span className="mv">2418 · 1066</span>
                <span className="mk">W · H</span><span className="mv">1180 · 1524</span>
                <span className="mk">Padding</span><span className="mv">30 px</span>
              </div>
            </div>
          </div>

          <Region n={5} label="accept / reset" pos="tl">
            <div className="redraw-actions">
              <button className="btn btn-primary btn-lg" style={{ flex: 1 }}><Icon d={ICONS.check} size={15} /> Accept Boundary</button>
              <button className="btn btn-lg" title="Reset to detected box"><Icon paths={ICONS.reset} size={15} /></button>
            </div>
          </Region>
          <div className="hint" style={{ textAlign: "center" }}>
            <span className="kbd">Enter</span> accept · <span className="kbd">R</span> reset · <span className="kbd">←/→</span> prev / next crop
          </div>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { RedrawBoundary });

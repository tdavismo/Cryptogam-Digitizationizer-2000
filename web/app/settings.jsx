/* global React */
/* Settings dropdown — skin picker + accessibility controls.
   Receives (t, setTweak) from App via props.
   Exports SettingsMenu to window for cross-scope access. */
const { useState, useEffect, useRef } = React;

const SKIN_OPTIONS = [
  { value: "wireframe", label: "Blueprint",
    colors: ["#0C2340","#1B4F8A","#5CC9DC","#ECECE4"] },
  { value: "vaporwave", label: "Vaporwave",
    colors: ["#160827","#FF3EAA","#5EE7FF","#2A1A4A"] },
  { value: "retro95",   label: "Retro '95",
    colors: ["#EDD9B8","#E76F51","#FFD166","#92C4E4"] },
];

function SkinCard({ opt, selected, onSelect }) {
  return (
    <button className={`skin-card ${selected ? "selected" : ""}`}
      onClick={() => onSelect(opt.value)} title={opt.label}>
      <div className="skin-swatches">
        {opt.colors.map((c, i) =>
          <div key={i} className="skin-swatch" style={{ background: c }} />
        )}
      </div>
      <span className="skin-name">{opt.label}</span>
      {selected && <span className="skin-check" aria-hidden="true">✓</span>}
    </button>
  );
}

function Toggle({ value, onChange }) {
  return (
    <button className={`sd-toggle ${value ? "on" : ""}`}
      role="switch" aria-checked={value}
      onClick={() => onChange(!value)}>
      <span className="sd-thumb" />
    </button>
  );
}

function SettingsMenu({ t, setTweak }) {
  const [open, setOpen] = useState(false);
  const [dropPos, setDropPos] = useState({ top: 0, right: 16 });
  const btnRef = useRef(null);
  const dropRef = useRef(null);

  /* Position dropdown below the gear button */
  function openMenu() {
    if (btnRef.current) {
      const r = btnRef.current.getBoundingClientRect();
      setDropPos({ top: r.bottom + 8, right: window.innerWidth - r.right });
    }
    setOpen(v => !v);
  }

  /* Close on outside click */
  useEffect(() => {
    if (!open) return;
    function onOut(e) {
      if (!btnRef.current?.contains(e.target) && !dropRef.current?.contains(e.target))
        setOpen(false);
    }
    document.addEventListener("mousedown", onOut);
    return () => document.removeEventListener("mousedown", onOut);
  }, [open]);

  /* Close on Escape */
  useEffect(() => {
    if (!open) return;
    function onKey(e) { if (e.key === "Escape") setOpen(false); }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <div className="settings-wrap">
      <button ref={btnRef}
        className={`settings-btn btn ${open ? "active" : ""}`}
        onClick={openMenu}
        aria-expanded={open}
        title="Settings">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none"
          style={{ flexShrink: 0 }}>
          <circle cx="8" cy="8" r="2.1" stroke="currentColor" strokeWidth="1.55" />
          <path d="M13.26 9.55a1 1 0 0 0 .2 1.1l.04.04a1.22 1.22 0 0 1-1.73 1.73l-.04-.04a1 1 0 0 0-1.1-.2 1 1 0 0 0-.6.91V13.2a1.2 1.2 0 0 1-2.4 0v-.07A1 1 0 0 0 6.98 12.02a1 1 0 0 0-1.1.2l-.04.04a1.22 1.22 0 0 1-1.73-1.73l.04-.04A1 1 0 0 0 4.35 9.4a1 1 0 0 0-.91-.6H2.8a1.2 1.2 0 0 1 0-2.4h.07A1 1 0 0 0 3.98 5.69a1 1 0 0 0-.2-1.1l-.04-.04A1.22 1.22 0 0 1 5.47 2.82l.04.04A1 1 0 0 0 6.6 3.06h.04A1 1 0 0 0 7.2 2.8V2.6a1.2 1.2 0 0 1 2.4 0v.07a1 1 0 0 0 .6.91h.04a1 1 0 0 0 1.1-.2l.04-.04a1.22 1.22 0 0 1 1.73 1.73l-.04.04a1 1 0 0 0-.2 1.1v.04a1 1 0 0 0 .91.6H13.2a1.2 1.2 0 0 1 0 2.4h-.07a1 1 0 0 0-.91.6Z"
            stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
        </svg>
        Settings
      </button>

      {open && (
        <div ref={dropRef} className="settings-dropdown"
          style={{ position: "fixed", top: dropPos.top, right: dropPos.right, zIndex: 300 }}>

          {/* Title bar */}
          <div className="sd-titlebar">
            <span className="sd-title">Settings</span>
            <button className="sd-close" onClick={() => setOpen(false)}
              aria-label="Close settings">✕</button>
          </div>

          <div className="sd-body">
            {/* ── Appearance ───────────────────────────────── */}
            <div className="sd-section-label">Appearance</div>
            <div className="sd-skin-grid">
              {SKIN_OPTIONS.map(opt =>
                <SkinCard key={opt.value} opt={opt}
                  selected={t.aesthetic === opt.value}
                  onSelect={v => setTweak("aesthetic", v)} />
              )}
            </div>

            <div className="sd-divider" />

            {/* ── Accessibility ────────────────────────────── */}
            <div className="sd-section-label">Accessibility</div>

            <div className="sd-field">
              <div className="sd-field-row">
                <label className="sd-label">Text size</label>
                <span className="sd-val">{t.fontSize || 100}%</span>
              </div>
              <div className="sd-slider-row">
                <span className="sd-hint-a small">A</span>
                <input type="range" className="slider" min="85" max="130" step="5"
                  value={t.fontSize || 100}
                  onChange={e => setTweak("fontSize", Number(e.target.value))} />
                <span className="sd-hint-a large">A</span>
              </div>
            </div>

            <div className="sd-toggle-row">
              <label className="sd-label">High contrast</label>
              <Toggle value={t.highContrast} onChange={v => setTweak("highContrast", v)} />
            </div>

            <div className="sd-toggle-row">
              <label className="sd-label">Reduce motion</label>
              <Toggle value={t.reduceMotion} onChange={v => setTweak("reduceMotion", v)} />
            </div>

            <div className="sd-toggle-row">
              <label className="sd-label">
                Legibility font
                <span className="sd-hint"> Atkinson Hyperlegible</span>
              </label>
              <Toggle value={t.legibilityFont} onChange={v => setTweak("legibilityFont", v)} />
            </div>
          </div>

          {/* Footer */}
          <div className="sd-footer">
            <button className="sd-reset btn btn-ghost"
              onClick={() => {
                setTweak("fontSize", 100);
                setTweak("highContrast", false);
                setTweak("reduceMotion", false);
                setTweak("legibilityFont", false);
              }}>
              Reset accessibility
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

Object.assign(window, { SettingsMenu });

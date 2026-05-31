/* global React, ReactDOM, useTweaks, TweaksPanel, TweakSection, TweakToggle, TweakColor, TweakSelect,
   FernMark, Icon, ICONS, SessionSetup, QCReview, RedrawBoundary, VVGoSubmission, SettingsMenu */
const { useState } = React;

const TABS = [
{ k: "setup", n: "01", label: "Session Setup", title: "Session Setup",
  sub: "Configure input/output folders and run segmentation. The preview shows the loaded scan with the detected packet outlines overlaid; the activity log streams processing status." },
{ k: "qc", n: "02", label: "QC Review", title: "QC — Segmentation Review",
  sub: "Browse every detected crop, approve good packets, and flag problems. Oversize crops (possible merges) and zero-detection images are surfaced automatically. Select a crop to inspect it." },
{ k: "redraw", n: "03", label: "Redraw Boundary", title: "Redraw Boundary",
  sub: "Manually correct a crop: drag the bounding-box handles to resize or reposition it over the full-resolution source region. The minimap shows where this crop sits in the parent scan." },
{ k: "submit", n: "04", label: "VVGo Submission", title: "VoucherVision Go — Submission",
  sub: "Submit approved crops to the VVGo API for automated transcription. Track per-crop status, watch overall progress, preview completed records, and retry any errors inline." }];

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "annotations": true,
  "annoColor": "#5CC9DC",
  "aesthetic": "vaporwave",
  "primary": "#C20430",
  "headingFont": "Source Serif 4",
  "fontSize": 100,
  "highContrast": false,
  "reduceMotion": false,
  "legibilityFont": false
} /*EDITMODE-END*/;

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const [tab, setTab] = useState("setup");

  /* Cross-screen navigation: QC's "Redraw" button needs to switch to the
     Redraw tab. Stash setTab on window so any screen can call it without
     wiring props through every level. */
  React.useEffect(() => { window.__CDZ_SET_TAB = setTab; }, []);

  /* Session Setup variant locked to "B" — SessionSetupA code is
     preserved in SessionSetup.jsx; change this constant to revive it. */
  const SETUP_VARIANT = "B";

  const active = TABS.find((x) => x.k === tab);

  /* Inline CSS-var overrides only apply in wireframe mode — the other
     skins own their full palette and font stacks via their own CSS. */
  const rootStyle = t.aesthetic === "wireframe" ? {
    "--anno": t.annoColor,
    "--red": t.primary,
    "--serif": `'${t.headingFont}', Georgia, serif`
  } : {};

  /* Accessibility — zoom the shell for text scaling (reliable in Chromium) */
  const shellStyle = t.fontSize && t.fontSize !== 100 ?
  { zoom: `${t.fontSize}%` } :
  undefined;

  const appClass = [
  "app",
  t.annotations ? "anno-on" : "",
  `aesthetic-${t.aesthetic}`,
  t.highContrast ? "high-contrast" : "",
  t.reduceMotion ? "reduce-motion" : "",
  t.legibilityFont ? "legibility-font" : ""].
  filter(Boolean).join(" ");

  return (
    <div className={appClass} style={{ ...rootStyle, borderStyle: "solid", borderWidth: "0px", padding: "0px", margin: "0px" }}>
      <div className="shell" style={shellStyle}>
        {/* Top bar */}
        <div className="topbar" data-comment-anchor="c495ac91b5-div-46-9">
          <div className="brand">
            <FernMark color="var(--green)" />
            <div className="brand-text">
              <span className="brand-uni">University of Guelph · Herbarium</span>
              <span className="brand-app">Cryptogam Digitizationizer <span className="ver">2000</span></span>
            </div>
          </div>
          <div className="topbar-end">
            <div className="topbar-meta">
              <span className="local"><span className="dot" /> localhost:8000 · FastAPI</span>
              <span>batch: lichen_2024_b3</span>
            </div>
            <SettingsMenu t={t} setTweak={setTweak} />
          </div>
        </div>

        {/* Tabs */}
        <div className="tabs" style={{ gap: "4px", borderWidth: "7px 150px 2px 0px", borderRadius: "0px", height: "36px", margin: "0px", padding: "40px 18px 0px" }}>
          {TABS.map((x) =>
          <button key={x.k} className={`tab ${tab === x.k ? "active" : ""}`} onClick={() => setTab(x.k)}>
              <span className="tnum">{x.n}</span>{x.label}
            </button>
          )}
        </div>

        {/* Screen */}
        <div className="screen" style={{ padding: "10px 26px 90px" }}>
          <div className="screen-head">
            <div style={{ height: "47px" }}>
              <div className="screen-kicker" style={{ opacity: "0" }}>Screen {active.n} · dev-reference wireframe</div>
              <div className="screen-title">{active.title}</div>
              <div className="screen-sub" style={{ textAlign: "left", margin: "-23px 0px 0px 175px", width: "800px", height: "20px", opacity: "0" }}>{active.sub}</div>
            </div>
          </div>

          {/* Session Setup stays permanently mounted (hidden when inactive) so
              an in-flight segmentation keeps streaming when the user visits
              another tab — conditional unmounting was silently killing the run
              (every setState from the live SSE loop became a no-op). While
              active we use display:contents so it stays out of the box tree and
              the layout/height chain is unchanged. The other three screens stay
              conditionally mounted so they re-fetch fresh on each open. */}
          <div style={{ display: tab === "setup" ? "contents" : "none" }}>
            <SessionSetup variant={SETUP_VARIANT} />
          </div>
          {tab === "qc" && <QCReview />}
          {tab === "redraw" && <RedrawBoundary />}
          {tab === "submit" && <VVGoSubmission />}
        </div>
      </div>

      <TweaksPanel>
        <TweakSection label="Annotations" />
        <TweakToggle label="Show region labels" value={t.annotations}
        onChange={(v) => setTweak("annotations", v)} />
        <TweakColor label="Annotation colour" value={t.annoColor}
        options={["#5CC9DC", "#FFC72A", "#B98BD9", "#8AD68A"]}
        onChange={(v) => setTweak("annoColor", v)} />

        <TweakSection label="Wireframe brand" />
        <TweakColor label="Primary / CTA" value={t.primary}
        options={["#C20430", "#A30329", "#7A1020", "#000000"]}
        onChange={(v) => setTweak("primary", v)} />
        <TweakSelect label="Heading face" value={t.headingFont}
        options={["Source Serif 4", "Source Sans 3", "IBM Plex Serif", "Georgia"]}
        onChange={(v) => setTweak("headingFont", v)} />
      </TweaksPanel>
    </div>);
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
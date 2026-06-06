/* Johnny landing — Tweaks panel.
   Applies live changes via CSS variables + body classes + persona text. */
const { useState, useEffect } = React;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "accent": "#f9e94e",
  "persona": "Johnny",
  "motion": "full",
  "density": "compact"
}/*EDITMODE-END*/;

// accent → [signal, deep, glow-rgb, ink]
const ACCENTS = {
  "#f9e94e": { deep: "#d6c50f", glow: "249,233,78",  ink: "#0a0a06" },  // acid yellow
  "#34e4d0": { deep: "#13bda9", glow: "52,228,208",  ink: "#04100e" },  // cyan
  "#ff2e63": { deep: "#d61446", glow: "255,46,99",   ink: "#160206" },  // magenta
  "#b6ff3c": { deep: "#8fd812", glow: "182,255,60",  ink: "#0a1203" }   // lime
};

const MOTION = { off: 0, subtle: 1, full: 2 };

function applyAccent(hex) {
  const a = ACCENTS[hex] || ACCENTS["#f9e94e"];
  const r = document.documentElement;
  r.style.setProperty("--signal", hex);
  r.style.setProperty("--signal-deep", a.deep);
  r.style.setProperty("--signal-ink", a.ink);
  r.style.setProperty("--glow", `rgba(${a.glow},0.55)`);
  r.style.setProperty("--glow-soft", `rgba(${a.glow},0.14)`);
}

function applyMotion(level) {
  const v = MOTION[level] ?? 1;
  document.documentElement.style.setProperty("--glitch", String(v));
  document.body.classList.toggle("full-glitch", v >= 2);
  document.body.classList.toggle("motion-off", v <= 0);
}

function applyDensity(d) {
  document.body.classList.remove("dense", "airy");
  if (d === "compact") document.body.classList.add("dense");
  if (d === "comfy") document.body.classList.add("airy");
}

function applyPersona(name) {
  const n = (name || "Johnny").trim() || "Johnny";
  document.querySelectorAll("[data-persona]").forEach((el) => {
    el.textContent = n;
  });
  document.querySelectorAll("[data-text]").forEach((el) => {
    el.setAttribute("data-text", n);
  });
  document.querySelectorAll("[data-persona-initial]").forEach((el) => {
    el.textContent = n.charAt(0).toUpperCase();
  });
}

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);

  useEffect(() => { applyAccent(t.accent); }, [t.accent]);
  useEffect(() => { applyMotion(t.motion); }, [t.motion]);
  useEffect(() => { applyDensity(t.density); }, [t.density]);
  useEffect(() => { applyPersona(t.persona); }, [t.persona]);

  return (
    <TweaksPanel title="Tweaks">
      <TweakSection label="Signal" />
      <TweakColor
        label="Accent"
        value={t.accent}
        options={["#f9e94e", "#34e4d0", "#ff2e63", "#b6ff3c"]}
        onChange={(v) => setTweak("accent", v)}
      />
      <TweakRadio
        label="Glitch / motion"
        value={t.motion}
        options={["off", "subtle", "full"]}
        onChange={(v) => setTweak("motion", v)}
      />
      <TweakSection label="Character" />
      <TweakText
        label="Persona name"
        value={t.persona}
        placeholder="Johnny"
        onChange={(v) => setTweak("persona", v)}
      />
      <TweakSection label="Layout" />
      <TweakRadio
        label="Density"
        value={t.density}
        options={["compact", "regular", "comfy"]}
        onChange={(v) => setTweak("density", v)}
      />
    </TweaksPanel>
  );
}

const host = document.createElement("div");
document.body.appendChild(host);
ReactDOM.createRoot(host).render(<App />);

# Product

## Register

product

## Users

The single technical operator who owns this Johnny instance. Comfortable in Docker, OAuth, provider keys, and `uv` / `pnpm`; runs Johnny on their own machine as a personal AI assistant. They open the UI between meetings to configure providers, queue Johnny against a calendar, review transcripts, and check on active sessions. They are simultaneously the admin, the user, and the only person who will ever see this surface — so the UI must respect their attention and never bury what's happening.

The primary job: see what Johnny is doing right now, get into and out of configuration quickly, and trust that an active meeting bot is behaving.

## Product Purpose

Johnny is a single-user AI assistant that joins Google Meet calls, transcribes them, and (within configured constraints) can speak on the user's behalf. The UI is the operator deck: provider/account configuration, calendar wiring, live session list with stop / approve controls, transcript history, templates, and a playground for STT/LLM/TTS experimentation.

Success looks like: at a glance, the operator knows whether the bot is in a call, who it's joined, and whether anything needs their decision. Configuration screens are precise and never ambiguous. Nothing is hidden behind chrome.

## Brand Personality

**Calm expert console with a committed signature.** Three words: composed, deliberate, signal.

Voice: confident, technical, terse. Sentences carry information, not enthusiasm. Labels say what will happen (`Stop session`, `Approve reply`), not how the user might feel about it. No exclamation marks. No "Powered by AI" copy. Microcopy reads like a tool, not a product.

Visual mood: Linear and Vercel's restraint as the chassis — dense layouts, hairline borders, tight type scale, near-zero ornament — but with a single committed identity color carried through dark surfaces. The palette anchors on **near-black + a saturated signal yellow** (Cyberpunk 2077-inspired: rgb(249, 233, 78) as the brand anchor). Yellow is a *signal*, not a *texture*: reserved for the things that matter (active session indicator, primary action, focused state, status badges that earn it). Most of the surface stays disciplined neutrals.

Dark is the primary identity (operator-tool feel, lower eye fatigue across long meetings). Light mode is functional and respected, but the screenshot, the README image, the brand impression all live in dark.

## Anti-references

Specific patterns and surfaces to refuse, even if a downstream prompt suggests them:

- **Generic shadcn-svelte default.** Black-and-white neutral, indigo accent, ring-2 focus, identical card stacks. The "I generated this in five minutes" look. The current `app.css` is exactly this — it ships as a baseline, not as the destination.
- **AI-startup landing-page energy on app screens.** Gradient hero, hero metrics, "Trusted by" logos, identical icon+heading+text card grids. Wrong register entirely.
- **Glitchy / pastiche cyberpunk.** Scanlines, chromatic aberration as decoration, glitch text effects, all-monospace body copy, neon outlines on every box, `FUTURE`/`2077`/`SYS://` all-caps labels. The Cyberpunk reference is for *color commitment* and *operator-deck confidence*, not a costume. Don't dress this up; let the palette and density carry it.
- **SaaS-marketing tropes on the dashboard.** Glassmorphism cards, big tracked uppercase eyebrows above every section, numbered `01 / 02 / 03` section markers, gradient text, side-stripe borders on alerts.
- **Friendly cartoon / mascot warmth.** No illustrations of robots, no emoji-heavy empty states, no "Hi! 👋" copy. Johnny is *named* but the UI doesn't anthropomorphize.
- **Enterprise admin clutter.** Three-pane shells with hairlines in every direction, ribbon toolbars, every-row-has-an-icon density. Density yes, decoration no.

## Design Principles

1. **Information first, ornament last.** Every pixel of chrome must earn its place against the data it surrounds. When in doubt, remove a border, a shadow, a gradient. If removal makes the screen unreadable, the chrome was load-bearing; otherwise it was decoration.
2. **One signature, one moment.** The yellow is a signal, not a texture. It appears on active state, on the primary CTA per surface, on the focus ring, on "this session is live right now" — and almost nowhere else. The discipline is what makes it feel intentional rather than themed.
3. **Dark as identity, light as accommodation.** Primary screenshots, the implicit "what does Johnny look like" answer, the first impression: dark. Light mode is engineered to be just as readable and just as on-brand, but it is not the front cover.
4. **Operator over visitor.** Every screen is designed for the person who has already understood it. No onboarding overlays on the main surface, no "Welcome back!" greetings, no progress-disclosure for users who are already past the disclosure. Empty states tell the operator what to do next in one sentence, then get out of the way.
5. **No pastiche, no nostalgia.** The references are tools (Linear, Vercel, Raycast) and a color brief (Cyberpunk 2077). They are not aesthetics to imitate. Surface treatments borrowed from the references' visual quirks (Linear's gradient mesh, Vercel's specific typography, Cyberpunk's CRT crunch) are off-limits unless they are demonstrably the best answer to a specific problem on a specific screen.

## Accessibility & Inclusion

Not a stated hard target; the operator is the only user. In practice the palette choice (near-black surfaces + L≈0.92 signal yellow) yields WCAG AA-equivalent contrast for body text and primary actions essentially for free, so this is the working baseline. Two operating commitments regardless of stated priority:

- **Reduced motion is honored.** Every transition has a `prefers-reduced-motion: reduce` fallback (typically a crossfade or instant change). Long meetings should not be visually fatiguing.
- **Color is never the only signal.** Active / error / success states use icon + label + color together, not color alone. The yellow signal must still mean "active" if the operator is color-blind, working under a blue-light filter, or screenshotting for a bug report.

WCAG AAA is not a stated goal but is the natural by-product on the dark theme; the light theme will be checked against AA when it gets serious attention.

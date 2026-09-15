# emtext website — shared design contract (read fully before writing code)

You are building ONE section of a single-page marketing site for **emtext**, a live
conversation interpreter that transcribes speech and reads its emotional subtext for a
neurodivergent listener who wants tone made explicit. The foundation (tokens, base
styles, nav, hero, footer) already exists and is coherent — your section must feel like
it was authored by the same hand. Do not restyle globals; consume the tokens.

## Aesthetic (match this exactly)
Deep, slightly-blue near-black canvas. Warm editorial serif (`Fraunces`, via
`--font-display`) for headlines and the human "read" text; clean `Inter`
(`--font-sans`, the default on body) for body/UI; `JetBrains Mono` (`--font-mono`) ONLY
for data, labels, transcripts, code-like chips. The five tone colors are load-bearing
brand and MUST keep their meaning: green=positive, red=negative, grey=neutral,
purple=sarcastic, amber=mixed. Chrome/interactive accent is a cool blue (`--accent`),
deliberately distinct from all tone colors — use it for buttons/links/focus, NOT to tag tone.

Calm, precise, confident. This is a thoughtful accessibility tool, not a hype SaaS.
AVOID generic-AI tells: no cream+terracotta, no acid-green, no uniform rounded card
grids, no tracked ALL-CAPS labels, no "→" glued onto every link, no single-accent-word
headlines. Structure (borders, dividers, real numbers) should encode meaning, not decorate.

## File contract (write EXACTLY these two files, nothing else)
- `src/sections/<name>.js`
- `src/sections/<name>.css`

The JS module MUST:
```js
import './<name>.css'
import { fromHTML } from '../lib/dom.js'   // returns one element from an HTML string

export function render<Name>() {
  const node = fromHTML(`
    <section class="section" id="<name>">
      <div class="wrap">
        ... your content ...
      </div>
    </section>
  `)
  // If interactive, wire listeners here, e.g. queueMicrotask(() => mount(node))
  return node
}
```
Return a single `<section class="section" id="...">` element. Do NOT touch `main.js`,
`tokens.css`, `base.css`, or any other section — those are already wired.

## Available tokens (use these variables — never raw hex)
Surfaces: `--ink --ink-2 --surface --surface-2 --surface-3 --edge --edge-soft`
Text: `--text --text-dim --muted --faint`
Tone: `--positive --negative --neutral --sarcastic --mixed` (+ `*-dim` soft tints)
Accent: `--accent --accent-hi --accent-dim --on-accent`
Signature gradient (positive→sarcastic): `--grad-mismatch`
Type scale: `--step--1 … --step-5`. Spacing: `--sp-1 … --sp-8`.
Radii: `--r-sm --r-md --r-lg --r-xl --r-pill`. Shadows: `--shadow-1 --shadow-2 --shadow-glow`.
Layout: `--maxw (1120px) --maxw-narrow (760px) --gutter`. Motion: `--ease --ease-out --dur`.

## Shared classes you SHOULD reuse (defined in base.css — do not redefine)
- `.section` (padding), `.wrap` / `.wrap-narrow` (centered max-width containers)
- `.eyebrow` — mono kicker with a leading rule. e.g. `<span class="eyebrow">how it works</span>`
- `.section-title` (Fraunces heading), `.section-lead` (dimmed intro paragraph)
- `.card` — surface + edge + radius panel
- `.btn .btn-primary` / `.btn .btn-ghost`
- `.tone-chip` with `data-tone="positive|negative|neutral|sarcastic|mixed"` — auto-colored pill
- Any element with `data-tone="..."` exposes `--tone` for you to reference in your CSS
- `.reveal` (+ optional `data-delay="1..4"`) on elements you want to fade/rise in on scroll.
  The observer is already global — just add the class; do not write your own observer for reveal.

## Rules
- Prefix ALL your custom CSS class names with your section name (e.g. `.pipeline-stage`)
  to avoid collisions. Only your two files are yours.
- Motion sparingly and orchestrated. Respect `@media (prefers-reduced-motion: reduce)`.
- Fully responsive; mobile-first. Content must never cause horizontal scroll. Wide
  tables/diagrams scroll inside their own `overflow-x:auto` container.
- Real content only — use the facts in your task prompt verbatim; invent no fake numbers,
  logos, testimonials, or company names.
- No new npm packages, no external CDNs/fonts/images. Inline SVG is fine and encouraged
  for icons/diagrams. No emoji as UI icons.
- Accessible: real heading levels (`h2` for the section title, `h3` for sub-items),
  `aria-label`/`aria-hidden` where needed, buttons for interactive controls.

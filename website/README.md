# emtext — product website

A single-page marketing site for **emtext**, the live conversation interpreter.
Built with **Vite** and plain ES modules — no framework, no runtime dependencies
beyond the bundled variable fonts.

## Develop

```bash
npm install
npm run dev      # http://localhost:5173
npm run build    # → dist/
npm run preview  # serve the production build
```

## How it's organized

The page is composed from independent section modules, each owning one
`.js` (markup + any interactivity) and one `.css` (scoped styles):

```
src/
  main.js              assembles the sections in order + the scroll-reveal observer
  styles/
    tokens.css         the whole design system: color, type, spacing, motion
    base.css           reset, fonts, shared primitives (.section .wrap .btn .tone-chip …)
  lib/dom.js           tiny helpers (fromHTML, tone metadata)
  sections/
    nav · hero · mismatch · pipeline · performance · pendant · privacy · footer
```

Every color, type step and spacing value comes from `styles/tokens.css`. The five
tone colors (green positive, red negative, grey neutral, purple sarcastic, amber
mixed) are load-bearing brand — they mean the same thing here as in the app.

`DESIGN_BRIEF.md` is the contract each section was built against; keep it in sync
if the design system changes.

Every figure on the site (latency budget, model accuracy, per-stage cost) is taken
verbatim from the project README and its measured evals — nothing is invented.

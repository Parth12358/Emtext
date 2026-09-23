import './nav.css'
import { fromHTML } from '../lib/dom.js'

export function renderNav() {
  return fromHTML(`
    <header class="nav">
      <nav class="nav-inner" aria-label="Primary">
        <a class="brand" href="#top" aria-label="emtext home">
          <span class="brand-mark" aria-hidden="true"><span></span><span></span></span>
          <span class="brand-name">emtext</span>
        </a>
        <div class="nav-links">
          <a data-navlink href="#mismatch">The idea</a>
          <a data-navlink href="#pipeline">How it works</a>
          <a data-navlink href="#performance">Measured</a>
          <a data-navlink href="#pendant">The pendant</a>
          <a data-navlink href="#privacy">Local-first</a>
        </div>
        <div class="nav-cta">
          <a class="nav-repo" href="https://github.com/Parth12358/emtext-firmware" rel="noopener">Firmware on GitHub</a>
          <a class="btn btn-primary" href="#pipeline">See it work</a>
        </div>
      </nav>
    </header>
  `)
}

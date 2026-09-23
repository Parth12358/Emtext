import './footer.css'
import { fromHTML } from '../lib/dom.js'

export function renderFooter() {
  return fromHTML(`
    <footer class="section footer" id="get-started">
      <div class="wrap">
        <div class="footer-cta reveal">
          <span class="eyebrow">self-hosted · firmware public</span>
          <h2>Make the subtext say its name.</h2>
          <p>
            Point a mic at it and start reading tone out loud. The browser works today; the
            pendant speaks the same protocol, and its firmware ships as flashable releases.
          </p>
          <div class="actions">
            <a class="btn btn-primary" href="#pipeline">How the pipeline works</a>
            <a class="btn btn-ghost" href="https://github.com/Parth12358/emtext-firmware" rel="noopener">Firmware on GitHub</a>
          </div>
        </div>

        <div class="footer-meta">
          <span class="brand-name">emtext</span>
          <div class="footer-legend" aria-label="Tone legend">
            <span data-tone="positive">positive</span>
            <span data-tone="negative">negative</span>
            <span data-tone="neutral">neutral</span>
            <span data-tone="sarcastic">sarcastic</span>
            <span data-tone="mixed">mixed</span>
          </div>
        </div>
        <p class="footer-note">
          A voice label is a hint, not a verdict — emtext guesses tone from acoustics and
          can be wrong. Every read says how sure the server was, and a shaky one is held
          back rather than dressed up. Evidence to weigh, not a fact about how someone feels.
        </p>
      </div>
    </footer>
  `)
}

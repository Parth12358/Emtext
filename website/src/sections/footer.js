import './footer.css'
import { fromHTML } from '../lib/dom.js'

export function renderFooter() {
  return fromHTML(`
    <footer class="section footer" id="get-started">
      <div class="wrap">
        <div class="footer-cta reveal">
          <span class="eyebrow">open source · self-hosted</span>
          <h2>Make the subtext say its name.</h2>
          <p>
            Clone it, point a mic at it, start reading tone out loud. The browser works
            today; a pendant speaks the same protocol.
          </p>
          <div class="actions">
            <a class="btn btn-primary" href="#pipeline">How the pipeline works</a>
            <a class="btn btn-ghost" href="#pendant">Meet the pendant</a>
          </div>
        </div>

        <div class="footer-meta">
          <span class="brand-name">C-ontext</span>
          <div class="footer-legend" aria-label="Tone legend">
            <span data-tone="positive">positive</span>
            <span data-tone="negative">negative</span>
            <span data-tone="neutral">neutral</span>
            <span data-tone="sarcastic">sarcastic</span>
            <span data-tone="mixed">mixed</span>
          </div>
        </div>
        <p class="footer-note">
          A voice label is a hint, not a verdict — C-ontext guesses tone from acoustics and
          can be wrong. Evidence to weigh, not a fact about how someone feels.
        </p>
      </div>
    </footer>
  `)
}

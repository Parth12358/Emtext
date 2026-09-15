import './mismatch.css'
import { fromHTML } from '../lib/dom.js'

// The whole point of this section: identical words, opposite meaning. The
// transcript below is frozen; only the DELIVERY changes, and every read
// (tone, color, and the voice-signal meters) is driven off that choice.
// valence runs 0 (negative) → 1 (positive); arousal runs 0 (calm) → 1 (intense).
const DELIVERIES = {
  warm: {
    key: 'warm',
    label: 'Warm delivery',
    tone: 'positive',
    emotion: 'happy',
    valence: 0.82,
    arousal: 0.55,
    read: 'They mean it. Genuinely pleased — the voice matches the words.',
  },
  flat: {
    key: 'flat',
    label: 'Flat, hostile delivery',
    tone: 'sarcastic',
    emotion: 'angry / flat',
    valence: 0.18,
    arousal: 0.6,
    read: 'The opposite of the words. This is frustration, not delight — read it as sarcasm.',
  },
}
const ORDER = ['warm', 'flat']

export function renderMismatch() {
  const node = fromHTML(`
    <section class="section mismatch" id="mismatch">
      <div class="wrap-narrow mismatch-head">
        <span class="eyebrow reveal">why the voice matters</span>
        <h2 class="section-title mismatch-title reveal" data-delay="1">The transcript can't tell them apart.</h2>
        <p class="section-lead reveal" data-delay="2">
          A genuine "oh, wonderful" and a bitter one are the same words — text can't tell
          them apart. C-ontext listens to the delivery and reads the gap between what was said
          and how it sounded.
        </p>
      </div>

      <div class="wrap mismatch-body">
        <div class="mismatch-demo card reveal" data-delay="1">
          <div class="mismatch-demo-head">
            <span class="mismatch-tag">same words · different delivery</span>
            <div class="mismatch-toggle" role="radiogroup" aria-label="Choose how the line is delivered">
              ${ORDER.map((k, i) => `
                <button
                  type="button"
                  class="mismatch-opt"
                  role="radio"
                  id="mm-opt-${k}"
                  data-key="${k}"
                  aria-checked="${i === 0 ? 'true' : 'false'}"
                  tabindex="${i === 0 ? '0' : '-1'}"
                >${DELIVERIES[k].label}</button>
              `).join('')}
            </div>
          </div>

          <p class="mismatch-transcript" id="mm-transcript">
            <span class="mismatch-quote" aria-hidden="true">“</span>Oh, wonderful. That's just perfect.<span class="mismatch-quote" aria-hidden="true">”</span>
          </p>

          <div class="mismatch-signal" aria-live="off">
            <div class="mismatch-signal-head">
              <span class="mismatch-signal-label">voice signal</span>
              <span class="mismatch-emotion" id="mm-emotion">happy</span>
            </div>
            <div class="mismatch-meters">
              <div class="mismatch-meter" data-axis="valence">
                <div class="mismatch-meter-row">
                  <span class="mismatch-meter-name">valence</span>
                  <span class="mismatch-meter-val" id="mm-valence-val">0.82</span>
                </div>
                <div class="mismatch-track" role="img" aria-label="valence, negative to positive">
                  <span class="mismatch-fill" id="mm-valence-fill"></span>
                  <span class="mismatch-dot" id="mm-valence-dot"></span>
                </div>
                <div class="mismatch-scale"><span>negative</span><span>positive</span></div>
              </div>
              <div class="mismatch-meter" data-axis="arousal">
                <div class="mismatch-meter-row">
                  <span class="mismatch-meter-name">arousal</span>
                  <span class="mismatch-meter-val" id="mm-arousal-val">0.55</span>
                </div>
                <div class="mismatch-track" role="img" aria-label="arousal, calm to intense">
                  <span class="mismatch-fill" id="mm-arousal-fill"></span>
                  <span class="mismatch-dot" id="mm-arousal-dot"></span>
                </div>
                <div class="mismatch-scale"><span>calm</span><span>intense</span></div>
              </div>
            </div>
          </div>

          <div class="mismatch-read" id="mm-read" data-tone="positive" aria-live="polite">
            <div class="mismatch-read-meta">
              <span class="mismatch-arrow" aria-hidden="true">${arrowSvg()}</span>
              <span class="tone-chip" id="mm-chip" data-tone="positive">positive</span>
              <span class="mismatch-read-label">interpreter reads</span>
            </div>
            <p class="mismatch-read-text" id="mm-read-text">${DELIVERIES.warm.read}</p>
          </div>
        </div>

        <div class="mismatch-rule reveal" data-delay="2">
          <h3 class="mismatch-rule-title">The rule it applies</h3>
          <p class="mismatch-rule-sub">When words and voice disagree, the gap itself carries the meaning.</p>
          <div class="mismatch-rule-scroll">
            <table class="mismatch-ruletable">
              <thead>
                <tr>
                  <th scope="col">words</th>
                  <th scope="col">voice</th>
                  <th scope="col">usually means</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td><span class="mismatch-cell-tag" data-tone="positive">positive</span></td>
                  <td class="mismatch-cell-voice mismatch-voice-neg">sounds negative</td>
                  <td>sarcasm, or masking that they're upset</td>
                </tr>
                <tr>
                  <td><span class="mismatch-cell-tag" data-tone="negative">negative</span></td>
                  <td class="mismatch-cell-voice mismatch-voice-pos">sounds positive</td>
                  <td>teasing, joking, banter</td>
                </tr>
                <tr>
                  <td><span class="mismatch-cell-tag" data-tone="neutral">they agree</span></td>
                  <td class="mismatch-cell-voice mismatch-voice-lit">sounds like agreement</td>
                  <td>take it literally</td>
                </tr>
              </tbody>
            </table>
          </div>
          <p class="mismatch-caution">
            The voice is a hint, not a verdict. Below ~0.4 confidence it counts as weak
            evidence at most — a voice label alone never turns a plain sentence into sarcasm.
          </p>
        </div>
      </div>
    </section>
  `)

  queueMicrotask(() => mount(node))
  return node
}

function arrowSvg() {
  return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14"/><path d="m19 12-7 7-7-7"/></svg>`
}

function mount(root) {
  const opts = [...root.querySelectorAll('.mismatch-opt')]
  const readEl = root.querySelector('#mm-read')
  const readText = root.querySelector('#mm-read-text')
  const chip = root.querySelector('#mm-chip')
  const emotionEl = root.querySelector('#mm-emotion')
  const vVal = root.querySelector('#mm-valence-val')
  const vFill = root.querySelector('#mm-valence-fill')
  const vDot = root.querySelector('#mm-valence-dot')
  const aVal = root.querySelector('#mm-arousal-val')
  const aFill = root.querySelector('#mm-arousal-fill')
  const aDot = root.querySelector('#mm-arousal-dot')

  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches
  let current = null

  function fmt(n) {
    return n.toFixed(2)
  }

  function apply(key, { animateText = true } = {}) {
    const d = DELIVERIES[key]
    if (!d || key === current) return
    current = key

    // toggle button state (radiogroup: exactly one checked/tabbable)
    opts.forEach((b) => {
      const on = b.dataset.key === key
      b.setAttribute('aria-checked', on ? 'true' : 'false')
      b.tabIndex = on ? 0 : -1
      b.classList.toggle('is-on', on)
    })

    // meters — the fill width and dot position both track the value directly
    const vPct = `${(d.valence * 100).toFixed(1)}%`
    const aPct = `${(d.arousal * 100).toFixed(1)}%`
    vFill.style.width = vPct
    vDot.style.left = vPct
    aFill.style.width = aPct
    aDot.style.left = aPct
    vVal.textContent = fmt(d.valence)
    aVal.textContent = fmt(d.arousal)
    emotionEl.textContent = d.emotion
    // tint the whole signal block by the tone it implies
    root.querySelector('.mismatch-demo').style.setProperty('--tone', `var(--${d.tone})`)

    // tone label + chip
    chip.dataset.tone = d.tone
    chip.textContent = d.tone
    readEl.dataset.tone = d.tone

    // smooth read-text swap
    if (animateText && !reduce) {
      readEl.classList.add('is-swapping')
      window.setTimeout(() => {
        readText.textContent = d.read
        readEl.classList.remove('is-swapping')
      }, 180)
    } else {
      readText.textContent = d.read
    }
  }

  opts.forEach((btn, i) => {
    btn.addEventListener('click', () => {
      apply(btn.dataset.key)
      btn.focus()
    })
    // arrow-key navigation for the radiogroup
    btn.addEventListener('keydown', (e) => {
      let next = null
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (i + 1) % opts.length
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (i - 1 + opts.length) % opts.length
      if (next === null) return
      e.preventDefault()
      const target = opts[next]
      apply(target.dataset.key)
      target.focus()
    })
  })

  // default to the warm state, no entry animation
  apply('warm', { animateText: false })
}

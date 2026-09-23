import './mismatch.css'
import { fromHTML } from '../lib/dom.js'

// The whole point of this section: identical words, opposite meaning. The
// transcript below is frozen; only the DELIVERY changes, and every read
// (tone, color, the voice-signal meters and the confidence chip) is driven off
// that choice.
//
// The meters are arousal and dominance, because that is what the shipped
// voice model (emotion2vec) actually grades: it has no valence axis, so the
// interpreter reads valence from the words themselves. Both run 0 → 1.
// The three deliveries are the three rows of the arousal + dominance rule.
const DELIVERIES = {
  warm: {
    key: 'warm',
    label: 'Warm',
    tone: 'positive',
    emotion: 'happy',
    arousal: 0.62,
    dominance: 0.58,
    conf: { score: 0.94, level: 'high' },
    read: 'They mean it. Genuinely pleased — the voice matches the words.',
  },
  flat: {
    key: 'flat',
    label: 'Hostile',
    tone: 'sarcastic',
    emotion: 'angry',
    arousal: 0.74,
    dominance: 0.79,
    conf: { score: 0.91, level: 'high' },
    read: 'The opposite of the words. Bright words on a hard, pushing voice — read it as sarcasm.',
  },
  small: {
    key: 'small',
    label: 'Quiet',
    tone: 'mixed',
    emotion: 'sad',
    arousal: 0.21,
    dominance: 0.24,
    conf: { score: 0.66, level: 'medium' },
    read: 'The words say wonderful; the voice has gone flat and small. Likely masking — they are not fine, and are keeping it polite.',
  },
}
const ORDER = ['warm', 'flat', 'small']

export function renderMismatch() {
  const node = fromHTML(`
    <section class="section mismatch" id="mismatch">
      <div class="wrap-narrow mismatch-head">
        <span class="eyebrow reveal">why the voice matters</span>
        <h2 class="section-title mismatch-title reveal" data-delay="1">The transcript can't tell them apart.</h2>
        <p class="section-lead reveal" data-delay="2">
          A genuine "oh, wonderful" and a bitter one are the same words — text can't tell
          them apart. emtext listens to the delivery and reads the gap between what was said
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
              <div class="mismatch-meter" data-axis="arousal">
                <div class="mismatch-meter-row">
                  <span class="mismatch-meter-name">arousal</span>
                  <span class="mismatch-meter-val" id="mm-arousal-val">0.62</span>
                </div>
                <div class="mismatch-track" role="img" aria-label="arousal, calm to intense">
                  <span class="mismatch-fill" id="mm-arousal-fill"></span>
                  <span class="mismatch-dot" id="mm-arousal-dot"></span>
                </div>
                <div class="mismatch-scale"><span>calm</span><span>intense</span></div>
              </div>
              <div class="mismatch-meter" data-axis="dominance">
                <div class="mismatch-meter-row">
                  <span class="mismatch-meter-name">dominance</span>
                  <span class="mismatch-meter-val" id="mm-dominance-val">0.58</span>
                </div>
                <div class="mismatch-track" role="img" aria-label="dominance, yielding to pushing">
                  <span class="mismatch-fill" id="mm-dominance-fill"></span>
                  <span class="mismatch-dot" id="mm-dominance-dot"></span>
                </div>
                <div class="mismatch-scale"><span>yielding</span><span>pushing</span></div>
              </div>
            </div>
          </div>

          <div class="mismatch-read" id="mm-read" data-tone="positive" aria-live="polite">
            <div class="mismatch-read-meta">
              <span class="mismatch-arrow" aria-hidden="true">${arrowSvg()}</span>
              <span class="tone-chip" id="mm-chip" data-tone="positive">positive</span>
              <span class="mismatch-read-label">interpreter reads</span>
              <span class="mismatch-conf" id="mm-conf" data-level="high" title="how sure the server was">conf 0.94</span>
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
                  <td class="mismatch-cell-voice mismatch-voice-neg">intense, pushing</td>
                  <td>sarcasm</td>
                </tr>
                <tr>
                  <td><span class="mismatch-cell-tag" data-tone="positive">positive</span></td>
                  <td class="mismatch-cell-voice mismatch-voice-neg">flat, small</td>
                  <td>masking that they're upset</td>
                </tr>
                <tr>
                  <td><span class="mismatch-cell-tag" data-tone="negative">negative</span></td>
                  <td class="mismatch-cell-voice mismatch-voice-neg">intense, pushing</td>
                  <td>real anger</td>
                </tr>
                <tr>
                  <td><span class="mismatch-cell-tag" data-tone="negative">negative</span></td>
                  <td class="mismatch-cell-voice mismatch-voice-neg">flat, small</td>
                  <td>resignation</td>
                </tr>
                <tr>
                  <td><span class="mismatch-cell-tag" data-tone="negative">negative</span></td>
                  <td class="mismatch-cell-voice mismatch-voice-pos">sounds warm</td>
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
          <p class="mismatch-backend">
            The shipped voice model grades arousal and dominance from its emotion distribution
            and has no valence axis; the interpreter reads valence from the words. A backend
            that measures all three (MERaLiON) is a switch away, at 16× the cost.
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
  const confEl = root.querySelector('#mm-conf')
  const emotionEl = root.querySelector('#mm-emotion')
  const aVal = root.querySelector('#mm-arousal-val')
  const aFill = root.querySelector('#mm-arousal-fill')
  const aDot = root.querySelector('#mm-arousal-dot')
  const dVal = root.querySelector('#mm-dominance-val')
  const dFill = root.querySelector('#mm-dominance-fill')
  const dDot = root.querySelector('#mm-dominance-dot')

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
    const aPct = `${(d.arousal * 100).toFixed(1)}%`
    const dPct = `${(d.dominance * 100).toFixed(1)}%`
    aFill.style.width = aPct
    aDot.style.left = aPct
    dFill.style.width = dPct
    dDot.style.left = dPct
    aVal.textContent = fmt(d.arousal)
    dVal.textContent = fmt(d.dominance)
    emotionEl.textContent = d.emotion
    // tint the whole signal block by the tone it implies
    root.querySelector('.mismatch-demo').style.setProperty('--tone', `var(--${d.tone})`)

    // tone label + chip + confidence
    chip.dataset.tone = d.tone
    chip.textContent = d.tone
    readEl.dataset.tone = d.tone
    confEl.dataset.level = d.conf.level
    confEl.textContent = `conf ${fmt(d.conf.score)}${d.conf.level === 'high' ? '' : ' · less sure'}`

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

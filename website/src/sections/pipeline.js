import './pipeline.css'
import { fromHTML } from '../lib/dom.js'
import { EXAMPLES } from './hero.js'
import { score as confidence } from '../lib/confidence.js'

// "How it works": the signal flow from mic to on-screen read, drawn as a
// vertical timeline. Every fact is verbatim from the product. The one
// architectural point the layout has to carry is that Transcribe, Read-the-voice
// and Whose-voice are a CONCURRENT trio (step 03) — shown as three cards side
// by side so an utterance costs about the max of the three, not their sum.

const DEVICE_LABEL = { client: 'client', cpu: 'CPU', gpu: 'GPU' }

function deviceChip(device) {
  return `<span class="pipeline-device" data-device="${device}">${DEVICE_LABEL[device]}</span>`
}

// Compact line-icons (no emoji, no external assets). 20px, stroke currentColor.
const ICONS = {
  capture: `<path d="M12 3a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Z"/><path d="M19 11a7 7 0 0 1-14 0"/><path d="M12 18v3"/>`,
  segment: `<path d="M2 12h3l2-6 3 12 3-9 2 3h5"/>`,
  transcribe: `<path d="M5 5h14"/><path d="M5 10h14"/><path d="M5 15h9"/><path d="M5 20h5"/>`,
  voice: `<path d="M4 12h2l1.5-5 2.5 11 2.5-9L16 12h4"/><circle cx="20" cy="12" r="0.6" fill="currentColor" stroke="none"/>`,
  who: `<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>`,
  interpret: `<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 3v2M14 3v2M10 19v2M14 19v2M3 10h2M3 14h2M19 10h2M19 14h2"/>`,
  screen: `<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>`,
}

function icon(name) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`
}

function meta(s) {
  const chips = []
  if (s.model) chips.push(`<span class="pipeline-model">${s.model}</span>`)
  if (s.cost) chips.push(`<span class="pipeline-cost">${s.cost}</span>`)
  return chips.length ? `<div class="pipeline-meta">${chips.join('')}</div>` : ''
}

// A single timeline step (one row: marker on the spine + content).
function step(s) {
  return `
    <li class="pipeline-step" data-step="${s.key}">
      <span class="pipeline-marker" aria-hidden="true">${s.num}</span>
      <div class="pipeline-step-main">
        <div class="pipeline-step-head">
          <span class="pipeline-icon">${icon(s.icon)}</span>
          <h3 class="pipeline-name">${s.name}</h3>
          ${deviceChip(s.device)}
        </div>
        <p class="pipeline-desc">${s.desc}</p>
        ${meta(s)}
        <span class="pipeline-done" aria-live="polite"></span>
      </div>
    </li>`
}

// One card inside the concurrent trio.
function card(s) {
  return `
    <div class="pipeline-card" data-card="${s.key}">
      <div class="pipeline-card-head">
        <span class="pipeline-icon pipeline-icon--sm">${icon(s.icon)}</span>
        <h4 class="pipeline-name pipeline-name--sm">${s.name}</h4>
      </div>
      <p class="pipeline-desc">${s.desc}</p>
      <div class="pipeline-meta">${deviceChip(s.device)}${s.model ? `<span class="pipeline-model">${s.model}</span>` : ''}${s.cost ? `<span class="pipeline-cost">${s.cost}</span>` : ''}</div>
    </div>`
}

export function renderPipeline() {
  const node = fromHTML(`
    <section class="section pipeline" id="pipeline">
      <div class="wrap">
        <header class="pipeline-head">
          <span class="eyebrow reveal">how it works</span>
          <h2 class="section-title pipeline-title reveal" data-delay="1">From a spoken sentence to its subtext, in one pass.</h2>
          <p class="section-lead reveal" data-delay="2">
            A client streams mic audio over a websocket. The server slices it into utterances,
            works out what was said, how it sounded and whose voice it was all at once, then
            asks a local model what it meant — and says how sure it is.
          </p>
        </header>

        <div class="pipeline-run card reveal" data-delay="2" id="pipeline-run">
          <div class="pipeline-run-head">
            <label class="pipeline-run-pick">
              <span class="pipeline-run-label">say a line</span>
              <select class="pipeline-run-select" id="pl-select" aria-label="Choose a line to run through the pipeline">
                ${EXAMPLES.map((e, i) => `<option value="${i}">${e.text}</option>`).join('')}
              </select>
            </label>
            <button type="button" class="btn btn-primary pipeline-run-btn" id="pl-run">Run it through</button>
            <div class="pipeline-clock" aria-live="off">
              <span class="pipeline-clock-n" id="pl-clock">0.00</span><span class="pipeline-clock-u">s</span>
              <span class="pipeline-clock-l" id="pl-clock-l">stopped talking</span>
            </div>
          </div>
          <div class="pipeline-result" id="pl-result" hidden>
            <p class="pipeline-result-text" id="pl-text"></p>
            <div class="pipeline-result-meta">
              <span class="tone-chip" id="pl-tone" data-tone="neutral">neutral</span>
              <span class="pipeline-conf" id="pl-conf"></span>
            </div>
            <p class="pipeline-result-read" id="pl-read"></p>
            <p class="pipeline-run-note">Real time. Each stage lights up for exactly as long as it was measured
              to take (the 1.94 s budget in the next section); the read and its confidence are
              what the server produced for this line.</p>
          </div>
        </div>

        <ol class="pipeline-flow reveal" data-delay="2">
          ${step({ key: 'capture', num: '01', name: 'Capture', icon: 'capture', device: 'client',
            desc: 'A browser AudioWorklet, or the pendant’s mic, streams 16 kHz mono int16 audio over a websocket.' })}

          ${step({ key: 'segment', num: '02', name: 'Segment', icon: 'segment', device: 'cpu', cost: 'negligible',
            desc: 'An energy-based VAD slices the stream into utterances. Its threshold tracks the room’s noise floor, so quiet, subdued speech is not dropped.' })}

          <li class="pipeline-step pipeline-step--concurrent" data-step="audio">
            <span class="pipeline-marker" aria-hidden="true">03</span>
            <div class="pipeline-step-main">
              <div class="pipeline-concurrent-tag">
                <span class="pipeline-tag">concurrent</span>
                <span class="pipeline-concurrent-note">all three run at once, so an utterance costs about
                  <b>max(0.10&nbsp;s, 0.12&nbsp;s, 0.08&nbsp;s)</b> — not their sum</span>
              </div>
              <span class="pipeline-done" aria-live="polite"></span>
              <div class="pipeline-pair">
                ${card({ key: 'asr', name: 'Transcribe', icon: 'transcribe', device: 'cpu',
                  model: 'Parakeet TDT 0.6B v3 · int8', cost: '~0.10 s',
                  desc: 'What was said.' })}
                ${card({ key: 'ser', name: 'Read the voice', icon: 'voice', device: 'cpu',
                  model: 'emotion2vec', cost: '~0.12 s',
                  desc: 'How it sounded.' })}
                ${card({ key: 'spk', name: 'Whose voice', icon: 'who', device: 'cpu',
                  model: 'CAM++', cost: '~0.08 s',
                  desc: 'Yours, or someone else’s.' })}
              </div>
            </div>
          </li>

          ${step({ key: 'interpret', num: '04', name: 'Interpret', icon: 'interpret', device: 'gpu',
            model: 'qwen3:14b · Ollama', cost: '~0.89 s',
            desc: 'A local model weighs the words against the voice and reads the subtext. Your own lines are kept as context and never interpreted — it must not explain your feelings back to you.' })}

          ${step({ key: 'screen', num: '05', name: 'Read on screen', icon: 'screen', device: 'client',
            desc: 'A color-coded tone read appears beneath the transcript, weighted by how sure the server was: normal, fainter, or held back behind a “not sure” mark.' })}
        </ol>

        <div class="pipeline-notes">
          <div class="pipeline-note card reveal" data-delay="1">
            <h3 class="pipeline-note-title">Concurrent, not sequential</h3>
            <p class="pipeline-note-body">
              Transcription, voice-reading and speaker id share one executor, so an utterance costs about
              <span class="pipeline-expr">max(asr,&nbsp;ser,&nbsp;speaker)</span>, not their sum.
            </p>
            <p class="pipeline-note-math" aria-hidden="true">
              <span class="pipeline-cost">max(0.10 s, 0.12 s, 0.08 s)</span>
              <span class="pipeline-not"> not </span>
              <span class="pipeline-sum">0.10 s + 0.12 s + 0.08 s</span>
            </p>
          </div>

          <div class="pipeline-note card reveal" data-delay="2">
            <h3 class="pipeline-note-title">The hardware split</h3>
            <p class="pipeline-note-body">
              Parakeet, the emotion model and speaker id stay on the
              ${deviceChip('cpu')}; the ${deviceChip('gpu')} is reserved entirely for the LLM.
            </p>
          </div>

          <div class="pipeline-note card reveal" data-delay="3">
            <h3 class="pipeline-note-title">One small contract</h3>
            <p class="pipeline-note-body">
              Raw PCM in; the same JSON frames back out — the wire an ESP32 pendant reuses unchanged.
              Hold the pendant’s button and the moment is saved as a clip.
            </p>
            <div class="pipeline-frames" aria-hidden="true">
              <span class="pipeline-frame">ready</span>
              <span class="pipeline-frame">status</span>
              <span class="pipeline-frame">utterance</span>
              <span class="pipeline-frame">read</span>
              <span class="pipeline-frame">saved</span>
            </div>
          </div>
        </div>
      </div>
    </section>
  `)
  queueMicrotask(() => mountRun(node))
  return node
}

// ---------------------------------------------------------------------------
// The run: plays the measured latency budget in real time over the timeline.
// Durations are the ones in eval/latency.py's budget for the default config:
// 0.65 s end-of-speech wait, 0.30 s gathered audio stages (each card fills for
// its own cost inside that window), 0.10 s handoff, 0.89 s LLM = 1.94 s.
// ---------------------------------------------------------------------------
const PLAN = [
  { key: 'capture', dur: 0, done: 'streaming' },
  { key: 'segment', dur: 0.65, done: '0.65 s wait for the pause' },
  { key: 'audio', dur: 0.30, done: '0.30 s, gathered',
    cards: [{ key: 'asr', dur: 0.10 }, { key: 'ser', dur: 0.12 }, { key: 'spk', dur: 0.08 }] },
  { key: 'handoff', dur: 0.10 },
  { key: 'interpret', dur: 0.89, done: '0.89 s on the GPU' },
  { key: 'screen', dur: 0, done: 'read on screen' },
]
const TOTAL = PLAN.reduce((a, p) => a + p.dur, 0)

// What the server measured for each demo line: the interpreter's first-token
// probability and the voiced length, which is what the confidence is built
// from. The last line is the short, strained one the server was not sure about.
const SIGNALS = [
  { toneProb: 0.96, voicedS: 2.3, asrConf: 0.93, serConf: 0.71 },
  { toneProb: 0.97, voicedS: 2.6, asrConf: 0.95, serConf: 0.83 },
  { toneProb: 0.98, voicedS: 2.0, asrConf: 0.91, serConf: 0.62 },
  { toneProb: 0.94, voicedS: 2.4, asrConf: 0.90, serConf: 0.55 },
  { toneProb: 0.72, voicedS: 1.1, asrConf: 0.88, serConf: 0.47 },
]

function mountRun(root) {
  const sel = root.querySelector('#pl-select')
  const btn = root.querySelector('#pl-run')
  const clock = root.querySelector('#pl-clock')
  const clockL = root.querySelector('#pl-clock-l')
  const result = root.querySelector('#pl-result')
  const steps = Object.fromEntries([...root.querySelectorAll('[data-step]')].map((el) => [el.dataset.step, el]))
  const cards = Object.fromEntries([...root.querySelectorAll('[data-card]')].map((el) => [el.dataset.card, el]))
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches
  let raf = 0

  function reset() {
    cancelAnimationFrame(raf)
    for (const el of [...Object.values(steps), ...Object.values(cards)]) {
      el.classList.remove('is-active', 'is-done')
      el.style.removeProperty('--p')
      const d = el.querySelector(':scope > .pipeline-step-main > .pipeline-done, :scope .pipeline-done')
      if (d && el.dataset.step) d.textContent = ''
    }
    result.hidden = true
    clock.textContent = '0.00'
    clockL.textContent = 'stopped talking'
  }

  function finish(ex, sig) {
    root.querySelector('#pl-text').textContent = '\u201c' + ex.text + '\u201d'
    // The headline is the emotion (the voice model's label as adjusted by the
    // interpreter); the tone chip only earns its place for sarcasm or mixed.
    const chip = root.querySelector('#pl-tone')
    chip.dataset.tone = ex.tone
    chip.textContent = ex.emotion ? ex.emotion + (ex.tone === 'sarcastic' || ex.tone === 'mixed' ? ' · ' + ex.tone : '') : ex.tone
    const conf = confidence({ tone: ex.tone, ...sig })
    const c = root.querySelector('#pl-conf')
    c.dataset.level = conf.level
    c.title = conf.reasons.length ? conf.reasons.join(', ') + ' \u00b7 score ' + conf.score : 'score ' + conf.score
    c.textContent = conf.level === 'high' ? 'conf ' + conf.score.toFixed(2)
      : conf.level === 'medium' ? 'less sure \u00b7 ' + conf.score.toFixed(2)
      : 'not sure \u00b7 ' + conf.score.toFixed(2)
    const read = root.querySelector('#pl-read')
    read.textContent = ex.read
    read.dataset.level = conf.level
    read.classList.toggle('is-hidden', conf.level === 'low')
    read.onclick = () => read.classList.remove('is-hidden')
    result.hidden = false
    clock.textContent = TOTAL.toFixed(2)
    clockL.textContent = 'read on screen'
  }

  function run() {
    reset()
    const ex = EXAMPLES[Number(sel.value)] || EXAMPLES[0]
    const sig = SIGNALS[Number(sel.value)] || SIGNALS[0]
    if (reduce) {
      for (const p of PLAN) {
        const el = steps[p.key]
        if (!el) continue
        el.classList.add('is-done')
        el.querySelector('.pipeline-done').textContent = p.done || ''
        for (const c of p.cards || []) cards[c.key].classList.add('is-done')
      }
      finish(ex, sig)
      return
    }
    btn.disabled = true
    const t0 = performance.now()
    let i = 0
    let stageStart = 0
    function frame(now) {
      const t = Math.max(0, now - t0) / 1000
      clock.textContent = Math.min(t, TOTAL).toFixed(2)
      while (i < PLAN.length && t >= stageStart + PLAN[i].dur) {
        const p = PLAN[i]
        const el = steps[p.key]
        if (el) {
          el.classList.remove('is-active')
          el.classList.add('is-done')
          el.style.setProperty('--p', '1')
          el.querySelector('.pipeline-done').textContent = p.done || ''
          for (const c of p.cards || []) {
            cards[c.key].classList.remove('is-active')
            cards[c.key].classList.add('is-done')
          }
        }
        stageStart += p.dur
        i++
      }
      if (i >= PLAN.length) {
        finish(ex, sig)
        btn.disabled = false
        return
      }
      const p = PLAN[i]
      const el = steps[p.key]
      const local = t - stageStart
      if (el) {
        el.classList.add('is-active')
        el.style.setProperty('--p', String(Math.min(1, local / p.dur)))
        for (const c of p.cards || []) {
          const ce = cards[c.key]
          const q = Math.min(1, local / c.dur)
          ce.classList.toggle('is-active', q < 1)
          ce.classList.toggle('is-done', q >= 1)
          ce.style.setProperty('--p', String(q))
        }
      }
      clockL.textContent = p.key === 'handoff' ? 'handing off' : p.key === 'segment' ? 'waiting for the pause' : p.key
      raf = requestAnimationFrame(frame)
    }
    raf = requestAnimationFrame(frame)
  }

  btn.addEventListener('click', run)
  sel.addEventListener('change', reset)
}

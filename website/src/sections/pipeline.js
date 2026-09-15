import './pipeline.css'
import { fromHTML } from '../lib/dom.js'

// "How it works": the signal flow from mic to on-screen read, drawn as a
// vertical timeline. Every fact is verbatim from the product. The one
// architectural point the layout has to carry is that Transcribe and
// Read-the-voice are a CONCURRENT pair (step 03) — shown as two cards side by
// side so an utterance costs about the max of the two, not their sum.

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
    <li class="pipeline-step">
      <span class="pipeline-marker" aria-hidden="true">${s.num}</span>
      <div class="pipeline-step-main">
        <div class="pipeline-step-head">
          <span class="pipeline-icon">${icon(s.icon)}</span>
          <h3 class="pipeline-name">${s.name}</h3>
          ${deviceChip(s.device)}
        </div>
        <p class="pipeline-desc">${s.desc}</p>
        ${meta(s)}
      </div>
    </li>`
}

// One card inside the concurrent pair.
function card(s) {
  return `
    <div class="pipeline-card">
      <div class="pipeline-card-head">
        <span class="pipeline-icon pipeline-icon--sm">${icon(s.icon)}</span>
        <h4 class="pipeline-name pipeline-name--sm">${s.name}</h4>
      </div>
      <p class="pipeline-desc">${s.desc}</p>
      <div class="pipeline-meta">${deviceChip(s.device)}${s.model ? `<span class="pipeline-model">${s.model}</span>` : ''}${s.cost ? `<span class="pipeline-cost">${s.cost}</span>` : ''}</div>
    </div>`
}

export function renderPipeline() {
  return fromHTML(`
    <section class="section pipeline" id="pipeline">
      <div class="wrap">
        <header class="pipeline-head">
          <span class="eyebrow reveal">how it works</span>
          <h2 class="section-title pipeline-title reveal" data-delay="1">From a spoken sentence to its subtext, in one pass.</h2>
          <p class="section-lead reveal" data-delay="2">
            A client streams mic audio over a websocket. The server slices it into utterances,
            works out what was said and how it sounded at once, then asks a local model what
            it meant.
          </p>
        </header>

        <ol class="pipeline-flow reveal" data-delay="2">
          ${step({ num: '01', name: 'Capture', icon: 'capture', device: 'client',
            desc: 'A browser AudioWorklet streams 16 kHz mono int16 audio over a websocket.' })}

          ${step({ num: '02', name: 'Segment', icon: 'segment', device: 'cpu', cost: 'negligible',
            desc: 'An energy-based VAD slices the continuous stream into utterances.' })}

          <li class="pipeline-step pipeline-step--concurrent">
            <span class="pipeline-marker" aria-hidden="true">03</span>
            <div class="pipeline-step-main">
              <div class="pipeline-concurrent-tag">
                <span class="pipeline-tag">concurrent</span>
                <span class="pipeline-concurrent-note">both run at once, so an utterance costs about
                  <b>max(0.24&nbsp;s, 0.12&nbsp;s)</b> — not their sum</span>
              </div>
              <div class="pipeline-pair">
                ${card({ name: 'Transcribe', icon: 'transcribe', device: 'cpu',
                  model: 'faster-whisper base · int8', cost: '~0.24 s',
                  desc: 'What was said.' })}
                ${card({ name: 'Read the voice', icon: 'voice', device: 'cpu',
                  model: 'emotion2vec', cost: '~0.12 s',
                  desc: 'How it sounded.' })}
              </div>
            </div>
          </li>

          ${step({ num: '04', name: 'Interpret', icon: 'interpret', device: 'gpu',
            model: 'qwen3:14b · Ollama', cost: '~0.89 s',
            desc: 'A local model weighs the words against the voice and reads the subtext.' })}

          ${step({ num: '05', name: 'Read on screen', icon: 'screen', device: 'client',
            desc: 'A color-coded tone read appears beneath the transcript.' })}
        </ol>

        <div class="pipeline-notes">
          <div class="pipeline-note card reveal" data-delay="1">
            <h3 class="pipeline-note-title">Concurrent, not sequential</h3>
            <p class="pipeline-note-body">
              Transcription and voice-reading share one executor, so an utterance costs about
              <span class="pipeline-expr">max(whisper,&nbsp;ser)</span>, not their sum.
            </p>
            <p class="pipeline-note-math" aria-hidden="true">
              <span class="pipeline-cost">max(0.24 s, 0.12 s)</span>
              <span class="pipeline-not"> not </span>
              <span class="pipeline-sum">0.24 s + 0.12 s</span>
            </p>
          </div>

          <div class="pipeline-note card reveal" data-delay="2">
            <h3 class="pipeline-note-title">The hardware split</h3>
            <p class="pipeline-note-body">
              Whisper and the emotion model stay on the
              ${deviceChip('cpu')}; the ${deviceChip('gpu')} is reserved entirely for the LLM.
            </p>
          </div>

          <div class="pipeline-note card reveal" data-delay="3">
            <h3 class="pipeline-note-title">One small contract</h3>
            <p class="pipeline-note-body">
              Raw PCM in; the same JSON frames back out — the wire an ESP32 pendant reuses unchanged.
            </p>
            <div class="pipeline-frames" aria-hidden="true">
              <span class="pipeline-frame">ready</span>
              <span class="pipeline-frame">status</span>
              <span class="pipeline-frame">utterance</span>
              <span class="pipeline-frame">read</span>
            </div>
          </div>
        </div>
      </div>
    </section>
  `)
}

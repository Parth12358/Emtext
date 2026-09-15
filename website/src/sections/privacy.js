import './privacy.css'
import { fromHTML } from '../lib/dom.js'

// Four real properties of the local-first pipeline. Icons are simple inline
// line SVGs (no emoji, no external assets) keyed by name below.
const CARDS = [
  {
    icon: 'shield',
    title: 'Nothing leaves your hardware',
    body:
      'Everything runs locally — faster-whisper and emotion2vec on the CPU, a local LLM on ' +
      'the GPU. No third-party API ever sees your audio.',
  },
  {
    icon: 'pulse',
    title: 'Degrades, never breaks',
    body:
      'If the LLM is unreachable the transcript still appears, read “(interpreter offline)”. ' +
      'A voice-model failure just drops the voice line. It degrades instead of crashing.',
  },
  {
    icon: 'tunnel',
    title: 'Reachable on your terms',
    body:
      'An optional Cloudflare quick tunnel serves the server over HTTPS in one command — no ' +
      'account, no domain. Also the only way to use a phone mic.',
  },
  {
    icon: 'key',
    title: 'One auth rule, everywhere',
    body:
      'One token gates the websocket and every API route. The exposure caps — message size, ' +
      'connections, inflight — are load-bearing, not decoration.',
  },
]

export function renderPrivacy() {
  const cardsHTML = CARDS.map(
    (c, i) => `
      <article class="card privacy-card reveal" data-delay="${(i % 4) + 1}">
        <span class="privacy-icon" aria-hidden="true">${icon(c.icon)}</span>
        <h3 class="privacy-card-title">${c.title}</h3>
        <p class="privacy-card-body">${c.body}</p>
      </article>`
  ).join('')

  const node = fromHTML(`
    <section class="section" id="privacy">
      <div class="wrap">
        <header class="privacy-head">
          <span class="eyebrow reveal">local-first</span>
          <h2 class="section-title privacy-title reveal" data-delay="1">Your conversations never leave your machine.</h2>
          <p class="section-lead privacy-lead reveal" data-delay="2">
            No cloud API, no account. Whisper and the emotion model run on your CPU, the LLM on
            your GPU via Ollama. Your audio stays on hardware you control.
          </p>
        </header>

        <div class="privacy-grid">
          ${cardsHTML}
        </div>

        <div class="privacy-where reveal" data-delay="2" role="note" aria-label="What runs where">
          <span class="privacy-where-item">
            <span class="privacy-where-k">CPU</span>
            <span class="privacy-where-v">Whisper + emotion model</span>
          </span>
          <span class="privacy-where-dot" aria-hidden="true"></span>
          <span class="privacy-where-item">
            <span class="privacy-where-k">GPU</span>
            <span class="privacy-where-v">the LLM</span>
          </span>
          <span class="privacy-where-dot" aria-hidden="true"></span>
          <span class="privacy-where-item">
            <span class="privacy-where-k privacy-where-k--none">nothing</span>
            <span class="privacy-where-v">the cloud</span>
          </span>
        </div>
      </div>
    </section>
  `)

  return node
}

// --- inline line icons (24x24, currentColor, stroke-based) ---
function icon(name) {
  const open = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
  const paths = {
    shield: '<path d="M12 3 5 6v5c0 4.2 2.9 7.6 7 9 4.1-1.4 7-4.8 7-9V6l-7-3Z"/><path d="m9 12 2 2 4-4"/>',
    pulse: '<path d="M3 12h4l2.5-6 4 12 2.5-6H21"/>',
    tunnel:
      '<path d="M3 20V11a9 9 0 0 1 18 0v9"/><path d="M8 20v-9a4 4 0 0 1 8 0v9"/><path d="M3 20h18"/>',
    key: '<circle cx="8" cy="12" r="4"/><path d="M11.5 12H21"/><path d="M17 12v3"/><path d="M20 12v2"/>',
  }
  return `${open}${paths[name] || ''}</svg>`
}

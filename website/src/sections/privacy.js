import './privacy.css'
import { fromHTML } from '../lib/dom.js'

// Six real properties of the local-first pipeline. Icons are simple inline
// line SVGs (no emoji, no external assets) keyed by name below.
const CARDS = [
  {
    icon: 'shield',
    title: 'Nothing leaves your hardware',
    body:
      'Everything runs locally — Parakeet, emotion2vec and the speaker model on the CPU, a ' +
      'local LLM on the GPU via Ollama. No third-party API ever sees your audio.',
  },
  {
    icon: 'person',
    title: 'Your own lines get no read',
    body:
      'Enrol your voice once and the server tells it from everyone else’s. Your lines become ' +
      'context for the next read and are never interpreted. The voiceprint is a few numbers ' +
      'in a local file — no audio is kept.',
  },
  {
    icon: 'pulse',
    title: 'Degrades, never breaks',
    body:
      'If the LLM is unreachable the transcript still appears, read “(interpreter offline)”. ' +
      'A voice-model failure just drops the voice line. A shaky read is marked “not sure” ' +
      'rather than dressed up.',
  },
  {
    icon: 'box',
    title: 'Nothing kept unless you ask',
    body:
      'Audio is processed and dropped. A clip is saved only while you hold the button, one ' +
      'bounded moment at a time. The transcript log is off by default, field by field, and ' +
      'says so when it would retain other people’s speech.',
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
      'connections, inflight, clips on disk — are load-bearing, not decoration.',
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
            No cloud API, no account. Parakeet, the emotion model and speaker id run on your CPU,
            the LLM on your GPU via Ollama. Your audio stays on hardware you control.
          </p>
        </header>

        <div class="privacy-grid">
          ${cardsHTML}
        </div>

        <div class="privacy-where reveal" data-delay="2" role="note" aria-label="What runs where">
          <span class="privacy-where-item">
            <span class="privacy-where-k">CPU</span>
            <span class="privacy-where-v">Parakeet + emotion + speaker id</span>
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
    person: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/><path d="M16 4l5 5"/><path d="M21 4l-5 5"/>',
    pulse: '<path d="M3 12h4l2.5-6 4 12 2.5-6H21"/>',
    box: '<path d="M3 8l9-5 9 5v8l-9 5-9-5Z"/><path d="M3 8l9 5 9-5"/><path d="M12 13v8"/>',
    tunnel:
      '<path d="M3 20V11a9 9 0 0 1 18 0v9"/><path d="M8 20v-9a4 4 0 0 1 8 0v9"/><path d="M3 20h18"/>',
    key: '<circle cx="8" cy="12" r="4"/><path d="M11.5 12H21"/><path d="M17 12v3"/><path d="M20 12v2"/>',
  }
  return `${open}${paths[name] || ''}</svg>`
}

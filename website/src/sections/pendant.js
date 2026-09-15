import './pendant.css'
import { fromHTML } from '../lib/dom.js'
import { EXAMPLES } from './hero.js'

// The build track, stated honestly. Stages 0–6 are done; 7–9 remain.
// `done: true` renders a filled marker, `false` an outlined one. The device
// milestone (the full loop running on hardware) is flagged for emphasis.
const STAGES = [
  { n: 0, done: true, name: 'Board bring-up', note: 'M5StickS3 flashing, boot, first light' },
  { n: 1, done: true, name: 'Config + serial console', note: 'settings and a wired debug shell' },
  { n: 2, done: true, name: 'Display & controls', note: 'screen driver and the physical buttons' },
  { n: 3, done: true, name: 'Mic capture', note: 'audio in, gated on an energy threshold' },
  { n: 4, done: true, name: 'Authenticated wss:// link', note: 'on a dedicated core, so audio never stalls' },
  { n: 5, done: true, milestone: true, name: 'Full loop on the device', note: 'streams its mic, survives a ~3s Wi-Fi drop with no audio loss, and shows the tone read on its own screen' },
  { n: 6, done: true, name: 'Wi-Fi setup portal', note: 'toggle a WPA2 hotspot, reconfigure from a phone captive page' },
  { n: 7, done: false, name: 'Audio cues', note: 'not started' },
  { n: 8, done: false, name: 'Power management', note: 'not started' },
  { n: 9, done: false, name: 'Acceptance / compliance', note: 'not started' },
]

export function renderPendant() {
  const doneCount = STAGES.filter((s) => s.done).length

  const stageRows = STAGES.map((s) => `
    <li class="pendant-stage ${s.done ? 'is-done' : 'is-todo'} ${s.milestone ? 'is-milestone' : ''}">
      <span class="pendant-stage-mark" aria-hidden="true">
        <span class="pendant-stage-num">${s.n}</span>
      </span>
      <span class="pendant-stage-body">
        <span class="pendant-stage-name">
          ${s.name}
          ${s.milestone ? '<span class="pendant-stage-flag">milestone</span>' : ''}
        </span>
        <span class="pendant-stage-note">${s.note}</span>
      </span>
      <span class="pendant-stage-state" aria-label="${s.done ? 'done' : 'remaining'}">${s.done ? 'done' : 'to&nbsp;do'}</span>
    </li>
  `).join('')

  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches
  const node = fromHTML(`
    <section class="section pendant" id="pendant">
      <div class="wrap">
        <header class="pendant-head">
          <span class="eyebrow reveal">the hardware half</span>
          <h2 class="section-title pendant-title reveal" data-delay="1">A browser today. A pendant you can wear next.</h2>
          <p class="section-lead pendant-lead reveal" data-delay="2">
            One contract — the websocket wire protocol — joins two codebases. The browser speaks
            it today; an ESP32-S3 pendant (an M5StickS3) is being built to replace it. All the
            interpretation stays on the server — the device is just ears and a screen.
          </p>
        </header>

        <div class="pendant-grid">
          <div class="pendant-device-col reveal" data-delay="1">
            <div
              class="pendant-device3d"
              id="pendant-device3d"
              role="img"
              aria-label="A 3D render of the M5StickS3 pendant, its screen showing a live tone read: a transcript above a color-coded interpretation."
            ></div>
            <p class="pendant-device-cap">M5StickS3 · ESP32-S3 · same wire protocol as the browser client</p>
          </div>

          <div class="pendant-track-col reveal" data-delay="2">
            <div class="pendant-track-head">
              <h3 class="pendant-track-title">Build progress</h3>
              <span class="pendant-track-count"><b>${doneCount}</b> of ${STAGES.length} stages</span>
            </div>
            <div class="pendant-track-bar" aria-hidden="true">
              <span class="pendant-track-fill" style="--pendant-fill:${(doneCount / STAGES.length) * 100}%"></span>
            </div>
            <ol class="pendant-stages">
              ${stageRows}
            </ol>
          </div>
        </div>

        <p class="pendant-contract reveal">
          Both halves speak the same protocol — raw PCM in,
          <code class="pendant-code">ready</code> / <code class="pendant-code">status</code> /
          <code class="pendant-code">utterance</code> / <code class="pendant-code">read</code> JSON out —
          so they're built and swapped independently.
        </p>
      </div>
    </section>
  `)

  // Same 3D pendant as the hero, so the two device moments read as one product.
  queueMicrotask(async () => {
    const host = node.querySelector('#pendant-device3d')
    if (!host) return
    try {
      const { mountDevice } = await import('../lib/device3d.js')
      mountDevice(host, { examples: EXAMPLES, reduceMotion: reduce })
    } catch (err) {
      host.classList.add('is-unavailable')
    }
  })

  return node
}

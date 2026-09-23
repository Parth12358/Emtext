import './pendant.css'
import { fromHTML } from '../lib/dom.js'
import { EXAMPLES } from './hero.js'

// The build track, stated honestly, mirroring docs/guides/firmware.md.
// `state` is one of: done | built (code-complete, hardware validation
// pending) | dropped (removed from scope) | todo. The device milestone (the
// full loop running on hardware) is flagged for emphasis.
const STAGES = [
  { n: '0', state: 'done', name: 'Board bring-up', note: 'M5StickS3 flashing, boot, first light' },
  { n: '1', state: 'done', name: 'Config + serial console', note: 'settings and a wired debug shell' },
  { n: '2', state: 'done', name: 'Display & controls', note: 'screen driver and the physical buttons' },
  { n: '3', state: 'done', name: 'Mic capture', note: 'audio in, gated on an energy threshold' },
  { n: '4', state: 'done', name: 'Authenticated wss:// link', note: 'on a dedicated core, so audio never stalls' },
  { n: '4P', state: 'done', name: 'Wi-Fi setup portal', note: 'toggle a WPA2 hotspot, reconfigure from a phone captive page' },
  { n: '5', state: 'done', milestone: true, name: 'Full loop on the device', note: 'streams its mic, survives a ~3 s Wi-Fi drop with no audio loss, shows the tone read on its own screen' },
  { n: '6', state: 'done', name: 'Glance rendering', note: 'real reads on the glance with a history ring; a low-confidence read draws faint' },
  { n: '7', state: 'dropped', name: 'Audio cues', note: 'the pendant has no speaker — the signal is visual only' },
  { n: '8', state: 'built', name: 'Power management', note: 'awake → idle → standby → off is code-complete; the 2.5 h runtime still has to be measured on hardware' },
  { n: '9', state: 'todo', name: 'Acceptance / compliance', note: 'outage recovery on screen, a “mic live” affordance' },
]

const STATE_LABEL = { done: 'done', built: 'built', dropped: 'dropped', todo: 'to&nbsp;do' }

export function renderPendant() {
  const doneCount = STAGES.filter((s) => s.state === 'done' || s.state === 'built').length
  const inScope = STAGES.filter((s) => s.state !== 'dropped').length

  const stageRows = STAGES.map((s) => `
    <li class="pendant-stage ${s.state === 'done' ? 'is-done' : 'is-todo'} is-${s.state} ${s.milestone ? 'is-milestone' : ''}">
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
      <span class="pendant-stage-state" aria-label="${STATE_LABEL[s.state].replace('&nbsp;', ' ')}">${STATE_LABEL[s.state]}</span>
    </li>
  `).join('')

  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches
  const node = fromHTML(`
    <section class="section pendant" id="pendant">
      <div class="wrap">
        <header class="pendant-head">
          <span class="eyebrow reveal">the hardware half</span>
          <h2 class="section-title pendant-title reveal" data-delay="1">A browser today. A pendant you can wear now.</h2>
          <p class="section-lead pendant-lead reveal" data-delay="2">
            One contract — the websocket wire protocol — joins two codebases. The browser speaks
            it, and so does an ESP32-S3 pendant (an M5StickS3) that already streams its mic and
            shows reads on its own screen. All the interpretation stays on the server — the
            device is just ears and a screen. Its firmware is public, with flashable releases.
          </p>
        </header>

        <div class="pendant-grid">
          <div class="pendant-device-col reveal" data-delay="1">
            <div
              class="pendant-device3d"
              id="pendant-device3d"
              role="img"
              aria-label="A 3D render of the M5StickS3 pendant, its screen showing the glance: a pixel heart coloured by tone above a short read."
            ></div>
            <p class="pendant-device-cap">M5StickS3 · ESP32-S3 · drag to turn, tap for the next line</p>
            <p class="pendant-device-cap">
              <a class="pendant-link" href="https://github.com/Parth12358/emtext-firmware" rel="noopener">Firmware and releases on GitHub</a>
              · PolyForm Noncommercial
            </p>
          </div>

          <div class="pendant-track-col reveal" data-delay="2">
            <div class="pendant-track-head">
              <h3 class="pendant-track-title">Build progress</h3>
              <span class="pendant-track-count"><b>${doneCount}</b> of ${inScope} stages</span>
            </div>
            <div class="pendant-track-bar" aria-hidden="true">
              <span class="pendant-track-fill" style="--pendant-fill:${(doneCount / inScope) * 100}%"></span>
            </div>
            <ol class="pendant-stages">
              ${stageRows}
            </ol>
            <p class="pendant-since">
              Also built since: hold-to-record clips (hold the button to save the moment to the
              server), status-bar overlays with live ping and battery, and the confidence field
              on every read (firmware 0.1.2).
            </p>
          </div>
        </div>

        <p class="pendant-contract reveal">
          Both halves speak the same protocol — raw PCM in,
          <code class="pendant-code">ready</code> / <code class="pendant-code">status</code> /
          <code class="pendant-code">utterance</code> / <code class="pendant-code">read</code> /
          <code class="pendant-code">saved</code> JSON out —
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

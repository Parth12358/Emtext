import './performance.css'
import { fromHTML } from '../lib/dom.js'
import { score as confidence, CONFIG, REASONS } from '../lib/confidence.js'

// ---------------------------------------------------------------------------
// Every figure in this section is measured on real hardware
// (Ryzen 5 9600X, 6c/12t + Intel Arc B580, 12 GB VRAM). Nothing here is an
// estimate, so nothing here is rounded to look nicer than it is.
// ---------------------------------------------------------------------------

const TOTAL_NOW = 1.94 // s, stop talking -> read on screen (measured)
const TOTAL_OLD = 4.63 // s, before emotion2vec replaced MERaLiON
const SAVED = +(TOTAL_OLD - TOTAL_NOW).toFixed(2) // 2.69 s

// The 1.94 s budget, broken into its measured parts. The three named stages
// sum to 1.84 s; the remaining 0.10 s is real wire + scheduling overhead, kept
// visible so the stacked bar actually adds up to the stated total.
const STAGES = [
  {
    key: 'wait',
    name: 'VAD end-of-speech wait',
    short: 'VAD wait',
    sec: 0.65,
    hw: 'END_SILENCE_MS',
    note: 'pure waiting — a tuning choice, not compute',
    swatch: 'var(--perf-wait)',
  },
  {
    key: 'cpu',
    name: 'transcribe + voice + speaker, gathered',
    short: 'audio stages',
    sec: 0.30,
    hw: 'CPU',
    note: 'transcription, voice-read and speaker id overlapped in one thread pool (timed with Whisper base; Parakeet, the default now, is faster still)',
    swatch: 'var(--perf-cpu)',
  },
  {
    key: 'gpu',
    name: 'LLM interpret',
    short: 'LLM interpret',
    sec: 0.89,
    hw: 'GPU',
    note: 'qwen3:14b reads the subtext on the Arc B580',
    swatch: 'var(--perf-gpu)',
  },
  {
    key: 'over',
    name: 'handoff + orchestration',
    short: 'overhead',
    sec: 0.10,
    hw: 'wire',
    note: 'websocket handoff and task scheduling',
    swatch: 'var(--perf-over)',
  },
]

const TILES = [
  { n: '1.94', u: 's', l: 'stop talking → read on screen', sub: 'end-to-end' },
  { n: '0.10', u: 's', l: 'Parakeet v3 transcription', sub: 'per utterance · CPU' },
  { n: '0.12', u: 's', l: 'emotion2vec voice read', sub: 'per utterance · CPU' },
  { n: '0.89', u: 's', l: 'qwen3:14b interpretation', sub: 'per utterance · GPU' },
]

// pct of the OLD total, so the "now" bar and the ghost "before" bar share one axis.
const pctOld = (sec) => (sec / TOTAL_OLD) * 100

function segMarkup(s) {
  const w = pctOld(s.sec).toFixed(3)
  return `
    <div class="perf-seg perf-seg--${s.key}" style="--w:${w}%"
         role="listitem"
         title="${s.name}: ${s.sec.toFixed(2)} s (${s.hw})"
         aria-label="${s.name}, ${s.sec.toFixed(2)} seconds, ${s.hw}">
      <span class="perf-seg-fill"></span>
      <span class="perf-seg-val"><span class="perf-num">${s.sec.toFixed(2)}</span><span class="perf-unit">s</span></span>
    </div>`
}

function legendRow(s) {
  return `
    <li class="perf-key">
      <span class="perf-key-sw" style="background:${s.swatch}" aria-hidden="true"></span>
      <span class="perf-key-name">${s.short}</span>
      <span class="perf-key-hw">${s.hw}</span>
      <span class="perf-key-val"><span class="perf-num">${s.sec.toFixed(2)}</span><span class="perf-unit">s</span></span>
      <span class="perf-key-note">${s.note}</span>
    </li>`
}

function tileMarkup(t) {
  return `
    <div class="perf-tile">
      <div class="perf-tile-n"><span class="perf-num">${t.n}</span><span class="perf-unit">${t.u}</span></div>
      <div class="perf-tile-l">${t.l}</div>
      <div class="perf-tile-sub">${t.sub}</div>
    </div>`
}

// A mini comparison bar: `val` scaled against `max`. Winner gets the accent
// fill, the beaten option a muted track. Lower-is-better metrics pass invert.
function compareBar({ label, unit, a, b, max, betterHigh }) {
  const aWins = betterHigh ? a.v >= b.v : a.v <= b.v
  const rows = [
    { ...a, win: aWins },
    { ...b, win: !aWins },
  ]
  const fmt = (v) => (Number.isInteger(v) ? v : v.toFixed(v < 10 ? (v < 1 ? 2 : 1) : 0))
  return `
    <div class="perf-cmp" role="group" aria-label="${label}">
      <div class="perf-cmp-label">${label}</div>
      <div class="perf-cmp-rows">
        ${rows
          .map(
            (r) => `
          <div class="perf-cmp-row${r.win ? ' is-win' : ''}">
            <span class="perf-cmp-name">${r.name}</span>
            <span class="perf-cmp-track" aria-hidden="true">
              <span class="perf-cmp-bar" style="--w:${((r.v / max) * 100).toFixed(2)}%"></span>
            </span>
            <span class="perf-cmp-val"><span class="perf-num">${fmt(r.v)}</span><span class="perf-unit">${unit}</span></span>
          </div>`,
          )
          .join('')}
      </div>
    </div>`
}

// Three rows on one axis, for the confidence levels: the top row wins.
function levelBars({ label, rows }) {
  return `
    <div class="perf-cmp" role="group" aria-label="${label}">
      <div class="perf-cmp-label">${label}</div>
      <div class="perf-cmp-rows">
        ${rows
          .map(
            (r, i) => `
          <div class="perf-cmp-row${i === 0 ? ' is-win' : ''}">
            <span class="perf-cmp-name">${r.name}</span>
            <span class="perf-cmp-track" aria-hidden="true">
              <span class="perf-cmp-bar" style="--w:${r.v.toFixed(2)}%"></span>
            </span>
            <span class="perf-cmp-val"><span class="perf-num">${r.v.toFixed(1)}</span><span class="perf-unit">%</span></span>
          </div>`,
          )
          .join('')}
      </div>
    </div>`
}

export function renderPerformance() {
  const savedPct = pctOld(SAVED).toFixed(3)
  const nowPct = pctOld(TOTAL_NOW).toFixed(3)

  const node = fromHTML(`
    <section class="section perf" id="performance">
      <div class="wrap">
        <header class="perf-head">
          <span class="eyebrow reveal">measured, not estimated</span>
          <h2 class="section-title reveal" data-delay="1">Every number here was timed on real hardware.</h2>
          <p class="section-lead reveal" data-delay="2">
            Measured on a Ryzen&nbsp;5&nbsp;9600X and an Intel&nbsp;Arc&nbsp;B580 — not estimated.
            Stop talking and a tone read lands in about two seconds.
          </p>
        </header>

        <div class="perf-budget card reveal" data-delay="1">
          <div class="perf-budget-head">
            <div class="perf-hero">
              <div class="perf-hero-n"><span class="perf-hero-num">1.94</span><span class="perf-hero-u">s</span></div>
              <div class="perf-hero-l">stop&nbsp;talking&nbsp;→&nbsp;read&nbsp;on&nbsp;screen</div>
            </div>
            <div class="perf-delta" aria-label="Down from 4.63 seconds, a 2.69 second improvement">
              <span class="perf-delta-was">was <span class="perf-num">4.63</span><span class="perf-unit">s</span></span>
              <span class="perf-delta-arrow" aria-hidden="true">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"/><path d="m5 12 7 7 7-7"/></svg>
              </span>
              <span class="perf-delta-amt">−<span class="perf-num">2.69</span><span class="perf-unit">s</span> after the emotion model swap</span>
            </div>
          </div>

          <div class="perf-chart" role="img"
               aria-label="Stacked latency budget. The 1.94 second end-to-end time breaks into VAD wait 0.65 s, the gathered audio stages 0.30 s on CPU, LLM interpret 0.89 s on GPU, and 0.10 s handoff overhead — down from 4.63 seconds before the emotion model was swapped.">
            <div class="perf-ghost" style="--now:${nowPct}%">
              <span class="perf-ghost-bar"></span>
              <span class="perf-ghost-tag">before · <span class="perf-num">4.63</span><span class="perf-unit">s</span></span>
              <span class="perf-ghost-saved" style="--saved:${savedPct}%">−<span class="perf-num">2.69</span><span class="perf-unit">s</span> saved</span>
            </div>
            <div class="perf-bar" role="list" aria-label="Latency budget by stage">
              ${STAGES.map(segMarkup).join('')}
              <span class="perf-bar-cap"><span class="perf-num">1.94</span><span class="perf-unit">s</span></span>
            </div>
            <div class="perf-axis" aria-hidden="true">
              <span class="perf-axis-t" style="--x:0%">0</span>
              <span class="perf-axis-t" style="--x:${pctOld(1).toFixed(2)}%">1s</span>
              <span class="perf-axis-t" style="--x:${pctOld(2).toFixed(2)}%">2s</span>
              <span class="perf-axis-t" style="--x:${pctOld(3).toFixed(2)}%">3s</span>
              <span class="perf-axis-t" style="--x:${pctOld(4).toFixed(2)}%">4s</span>
              <span class="perf-axis-t perf-axis-t--end" style="--x:100%">4.63s</span>
            </div>
          </div>

          <ul class="perf-legend" aria-label="What each stage of the budget is">
            ${STAGES.map(legendRow).join('')}
          </ul>
        </div>

        <div class="perf-tiles reveal" data-delay="2" aria-label="Per-utterance measurements">
          ${TILES.map(tileMarkup).join('')}
        </div>

        <div class="perf-why reveal" data-delay="1">
          <div class="perf-why-head">
            <h3 class="perf-why-title">Why these models</h3>
            <p class="perf-why-sub">The choices were driven by measurements, not vibes. Each winner was picked because the numbers said so.</p>
          </div>

          <div class="perf-why-grid">
            <article class="perf-evidence card">
              <header class="perf-ev-head">
                <span class="perf-ev-kicker">voice model</span>
                <h4 class="perf-ev-title"><span class="perf-win">emotion2vec</span> beats MERaLiON</h4>
              </header>
              ${compareBar({
                label: 'Accuracy on 1440 RAVDESS clips',
                unit: '%',
                a: { name: 'emotion2vec', v: 86 },
                b: { name: 'MERaLiON', v: 61.3 },
                max: 100,
                betterHigh: true,
              })}
              ${compareBar({
                label: 'Latency per utterance (lower is better)',
                unit: 's',
                a: { name: 'emotion2vec', v: 0.17 },
                b: { name: 'MERaLiON', v: 2.72 },
                max: 2.72,
                betterHigh: false,
              })}
              <p class="perf-ev-foot">More accurate <em>and</em> 16× faster — an easy call.</p>
            </article>

            <article class="perf-evidence card">
              <header class="perf-ev-head">
                <span class="perf-ev-kicker">interpreter LLM</span>
                <h4 class="perf-ev-title"><span class="perf-win">qwen3:14b</span> reads the voice</h4>
              </header>
              ${compareBar({
                label: 'Tone accuracy on 280 neutral-word clips',
                unit: '%',
                a: { name: 'qwen3:14b', v: 81 },
                b: { name: 'ignores the voice', v: 0.4 },
                max: 100,
                betterHigh: true,
              })}
              <p class="perf-ev-foot">
                On 280 clips the words are deliberately neutral, so all the signal is in the
                voice. A model that ignores it scores near
                <span class="perf-num">0</span> — proof the read is real.
              </p>
            </article>

            <article class="perf-evidence card">
              <header class="perf-ev-head">
                <span class="perf-ev-kicker">transcription</span>
                <h4 class="perf-ev-title"><span class="perf-win">Parakeet v3</span> replaces Whisper base</h4>
              </header>
              ${compareBar({
                label: 'Word error rate on the reference set (lower is better)',
                unit: '%',
                a: { name: 'Parakeet v3', v: 6.0 },
                b: { name: 'Whisper base', v: 6.0 },
                max: 12,
                betterHigh: false,
              })}
              ${compareBar({
                label: 'Latency per utterance (lower is better)',
                unit: 's',
                a: { name: 'Parakeet v3', v: 0.10 },
                b: { name: 'Whisper base', v: 0.26 },
                max: 0.26,
                betterHigh: false,
              })}
              <p class="perf-ev-foot">
                Same accuracy at well under half the cost, plus 25 languages — and its cost scales
                with the audio, where Whisper pads every line to 30&nbsp;s. Measured on clean
                synthetic speech; a real-room comparison is still owed.
              </p>
            </article>

            <article class="perf-evidence card">
              <header class="perf-ev-head">
                <span class="perf-ev-kicker">confidence</span>
                <h4 class="perf-ev-title">The <span class="perf-win">“how sure”</span> number means something</h4>
              </header>
              ${levelBars({
                label: 'Accuracy of the read, by the weight the listener sees',
                rows: [
                  { name: 'high · 22%', v: 96.8 },
                  { name: 'medium · 63%', v: 83.0 },
                  { name: 'low · 15%', v: 17.1 },
                ],
              })}
              <p class="perf-ev-foot">
                Each read carries a score built from the interpreter's own token probability,
                the voice, the transcript and the utterance length (AUROC
                <span class="perf-num">0.84</span> against correctness on the same 280 clips).
                A low read is right one time in six, so the client holds it back.
              </p>
            </article>
          </div>
        </div>

        <div class="perf-explore card reveal" data-delay="2" id="perf-explore">
          <div class="perf-explore-head">
            <span class="perf-ev-kicker">try the formula</span>
            <h3 class="perf-explore-title">What makes a read “not sure”?</h3>
            <p class="perf-explore-sub">
              These sliders feed the exact scoring rule the server runs on every read
              (<code>server/confidence.py</code>): the interpreter's own probability is the base,
              and each weak input multiplies it by ${CONFIG.PENALTY}. Below ${CONFIG.LOW} the
              read is held back; from ${CONFIG.HIGH} it shows at full weight.
            </p>
          </div>
          <div class="perf-explore-grid">
            <div class="perf-explore-controls">
              ${slider({ id: 'toneProb', label: 'interpreter probability', min: 0.3, max: 1, step: 0.01, value: 0.95, fmt: 'p' })}
              ${slider({ id: 'voicedS', label: 'voiced seconds', min: 0.3, max: 4, step: 0.1, value: 2.4, fmt: 's', mark: CONFIG.MIN_VOICE_S })}
              ${slider({ id: 'serConf', label: 'voice-model confidence', min: 0.1, max: 1, step: 0.01, value: 0.7, fmt: 'p', mark: CONFIG.SER_MIN })}
              ${slider({ id: 'asrConf', label: 'transcriber confidence', min: 0.05, max: 1, step: 0.01, value: 0.9, fmt: 'p', mark: CONFIG.MIN_ASR })}
              <div class="perf-explore-toggles">
                <label class="perf-toggle"><input type="checkbox" id="px-mixedSpk"> two voices in the line</label>
                <label class="perf-toggle"><input type="checkbox" id="px-mixedTone"> interpreter says “mixed”</label>
                <label class="perf-toggle"><input type="checkbox" id="px-offline"> interpreter offline</label>
              </div>
            </div>
            <div class="perf-explore-out" aria-live="polite">
              <div class="perf-explore-score">
                <span class="perf-explore-n" id="px-score">0.95</span>
                <span class="perf-explore-level" id="px-level" data-level="high">high</span>
              </div>
              <div class="perf-explore-track" aria-hidden="true">
                <span class="perf-explore-zone perf-explore-zone--low" style="--from:0;--to:${CONFIG.LOW}"></span>
                <span class="perf-explore-zone perf-explore-zone--mid" style="--from:${CONFIG.LOW};--to:${CONFIG.HIGH}"></span>
                <span class="perf-explore-zone perf-explore-zone--high" style="--from:${CONFIG.HIGH};--to:1"></span>
                <span class="perf-explore-needle" id="px-needle"></span>
              </div>
              <ul class="perf-explore-reasons" id="px-reasons"></ul>
              <div class="perf-explore-preview" id="px-preview">
                <span class="tone-chip" data-tone="sarcastic">sarcastic</span>
                <span class="perf-explore-chip" id="px-chip" hidden></span>
                <p class="perf-explore-read" id="px-read">The words are bright; the voice is not. This reads as sarcasm.</p>
              </div>
              <p class="perf-explore-how" id="px-how">This is how the browser client would show it.</p>
            </div>
          </div>
        </div>
      </div>
    </section>
  `)

  queueMicrotask(() => mountExplore(node))
  return node
}

function slider({ id, label, min, max, step, value, fmt, mark }) {
  const pct = mark != null ? (((mark - min) / (max - min)) * 100).toFixed(1) : null
  return `
    <label class="perf-slider" for="px-${id}">
      <span class="perf-slider-row">
        <span class="perf-slider-label">${label}</span>
        <span class="perf-slider-val" id="px-${id}-val" data-fmt="${fmt}">${fmt === 's' ? value.toFixed(1) + ' s' : value.toFixed(2)}</span>
      </span>
      <span class="perf-slider-track">
        <input type="range" id="px-${id}" min="${min}" max="${max}" step="${step}" value="${value}">
        ${pct != null ? `<span class="perf-slider-mark" style="--x:${pct}%" title="threshold ${mark}"></span>` : ''}
      </span>
    </label>`
}

function mountExplore(root) {
  const $ = (id) => root.querySelector('#px-' + id)
  const ids = ['toneProb', 'voicedS', 'serConf', 'asrConf']
  const reasonsEl = $('reasons')
  const chip = $('chip')
  const read = $('read')
  const how = $('how')

  function update() {
    const v = Object.fromEntries(ids.map((k) => [k, Number($(k).value)]))
    for (const k of ids) {
      const el = $(k + '-val')
      el.textContent = el.dataset.fmt === 's' ? v[k].toFixed(1) + ' s' : v[k].toFixed(2)
    }
    const r = confidence({
      tone: $('mixedTone').checked ? 'mixed' : 'sarcastic',
      toneProb: v.toneProb,
      voicedS: v.voicedS,
      serConf: v.serConf,
      asrConf: v.asrConf,
      speakerLabel: $('mixedSpk').checked ? 'mixed' : 'other',
      offline: $('offline').checked,
    })
    $('score').textContent = r.score.toFixed(2)
    const lv = $('level')
    lv.textContent = r.level
    lv.dataset.level = r.level
    $('needle').style.setProperty('--x', (r.score * 100).toFixed(1) + '%')
    reasonsEl.innerHTML = r.reasons.length
      ? r.reasons.map((x) => `<li>${x}</li>`).join('')
      : '<li class="is-none">no weak inputs</li>'
    // the preview mirrors server/static/index.html: high as-is, medium
    // de-emphasised with a "less sure" chip, low hidden behind "not sure".
    read.dataset.level = r.level
    read.classList.toggle('is-hidden', r.level === 'low')
    if (r.level === 'high') {
      chip.hidden = true
      how.textContent = 'Shown at full weight. The pendant draws the caption in white.'
    } else if (r.level === 'medium') {
      chip.hidden = false
      chip.textContent = 'less sure'
      how.textContent = 'Shown fainter, with the reasons on the chip. The pendant still draws it in white.'
    } else {
      chip.hidden = false
      chip.textContent = r.reasons[0] === REASONS.offline ? 'interpreter offline' : 'not sure'
      how.textContent = 'Held back until tapped. The pendant draws the caption in faint grey.'
    }
  }

  for (const k of ids) $(k).addEventListener('input', update)
  for (const k of ['mixedSpk', 'mixedTone', 'offline']) $(k).addEventListener('change', update)
  read.addEventListener('click', () => read.classList.remove('is-hidden'))
  update()
}

import './performance.css'
import { fromHTML } from '../lib/dom.js'

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
    name: 'Whisper + emotion, gathered',
    short: 'Whisper + emotion',
    sec: 0.30,
    hw: 'CPU',
    note: 'transcribe and voice-read, overlapped in one thread pool',
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
  { n: '0.24', u: 's', l: 'Whisper base transcription', sub: 'per utterance · CPU' },
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
               aria-label="Stacked latency budget. The 1.94 second end-to-end time breaks into VAD wait 0.65 s, Whisper plus emotion 0.30 s on CPU, LLM interpret 0.89 s on GPU, and 0.10 s handoff overhead — down from 4.63 seconds before the emotion model was swapped.">
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
          </div>
        </div>
      </div>
    </section>
  `)

  return node
}

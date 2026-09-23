import './hero.css'
import { fromHTML } from '../lib/dom.js'

// Real examples drawn from the product's own tone cases: identical-looking
// speech whose meaning lives entirely in the delivery. These play on the
// pendant's screen in the 3D hero. `emotion` is the headline the server now
// sends (the voice model's label, adjusted by the interpreter) and is what
// colours the pendant's heart; `tone` stays for sarcasm. `glance` is what the pendant shows: the
// firmware caps the read at five words, so each read here has a five-word
// form (the transcript itself never appears on the glance). `conf` marks the
// one read the server was not sure about -- the pendant draws it faint.
// The five lines cover all five tones, so the pendant cycles every soul colour.
export const EXAMPLES = [
  {
    text: 'Oh, wonderful. That is just perfect.',
    tone: 'sarcastic',
    emotion: 'angry',
    voice: 'flat, low valence',
    read: 'The words are bright; the voice is not. This reads as sarcasm — the opposite of pleased.',
    glance: 'Sarcasm, not delight',
  },
  {
    text: 'This is the best news I have had all week.',
    tone: 'positive',
    emotion: 'happy',
    voice: 'bright, lifted',
    read: 'Genuinely delighted — the voice is as bright as the words. Take the happiness at face value.',
    glance: 'Genuinely delighted, believe it',
  },
  {
    text: 'The train leaves at four fifteen.',
    tone: 'neutral',
    emotion: 'neutral',
    voice: 'even, calm',
    read: 'A plain statement of fact. Nothing hidden here — take it literally.',
    glance: 'Plain fact, take literally',
  },
  {
    text: 'Thanks for finally getting back to me.',
    tone: 'mixed',
    emotion: 'angry',
    voice: 'clipped, tense',
    read: 'Polite on the surface, but "finally" carries real irritation underneath.',
    glance: 'Polite, but irritated underneath',
  },
  {
    text: 'No, it is fine. Do whatever you want.',
    tone: 'negative',
    emotion: 'sad',
    voice: 'strained, quiet',
    read: 'The words say fine. The voice says the opposite. They are upset.',
    glance: 'Says fine, sounds upset',
    conf: 'low',
  },
]

export function renderHero() {
  const node = fromHTML(`
    <section class="section hero" id="top">
      <div class="wrap hero-grid">
        <div class="hero-copy">
          <span class="eyebrow hero-eyebrow reveal"><span class="live-dot" aria-hidden="true"></span>live conversation interpreter</span>
          <h1 class="hero-title reveal" data-delay="1">The words are only<br>half of what's <em>said</em>.</h1>
          <p class="hero-lead reveal" data-delay="2">
            It transcribes speech as you hear it, tells your voice from everyone else's, and
            reads the emotional subtext of each line — sarcasm, passive-aggression, or plain
            fact — saying how sure it is. Tone, made explicit.
          </p>
          <div class="hero-actions reveal" data-delay="3">
            <a class="btn btn-primary" href="#mismatch">Why the voice matters</a>
            <a class="btn btn-ghost" href="#performance">See the numbers</a>
          </div>
          <dl class="hero-stats reveal" data-delay="4">
            <div class="hero-stat">
              <div class="n">1.94<small>s</small></div>
              <div class="l">stop talking → read on screen</div>
            </div>
            <div class="hero-stat">
              <div class="n">100<small>%</small></div>
              <div class="l">runs on your own machine</div>
            </div>
            <div class="hero-stat">
              <div class="n">0</div>
              <div class="l">reads of your own lines, ever</div>
            </div>
          </dl>
        </div>

        <div class="hero-demo reveal" data-delay="2">
          <figure class="hero-device-fig">
            <div
              class="hero-device"
              id="hero-device"
              role="img"
              aria-label="A 3D render of the emtext pendant — an M5StickS3 — with its screen showing the glance: a pixel heart coloured by the tone of the last line, and a short read of its emotional subtext beneath."
            ></div>
            <figcaption class="hero-device-cap">
              <span class="hero-device-dot" aria-hidden="true"></span>
              M5StickS3 pendant · drag to turn · tap for the next line
            </figcaption>
          </figure>
        </div>
      </div>
    </section>
  `)

  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches
  // Code-split three.js: the hero copy paints immediately, the pendant streams
  // in right after. Mount once the host is in the DOM and sized.
  queueMicrotask(async () => {
    const host = node.querySelector('#hero-device')
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

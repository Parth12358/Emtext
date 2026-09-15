import './styles/base.css'

import { renderNav } from './sections/nav.js'
import { renderHero } from './sections/hero.js'
import { renderMismatch } from './sections/mismatch.js'
import { renderPipeline } from './sections/pipeline.js'
import { renderPerformance } from './sections/performance.js'
import { renderPendant } from './sections/pendant.js'
import { renderPrivacy } from './sections/privacy.js'
import { renderFooter } from './sections/footer.js'

const app = document.getElementById('app')

// Order defines the page narrative:
// hook → the core insight (mismatch) → how it works → proof (numbers) →
// the hardware future → local-first trust → footer.
const sections = [
  renderNav(),
  renderHero(),
  renderMismatch(),
  renderPipeline(),
  renderPerformance(),
  renderPendant(),
  renderPrivacy(),
  renderFooter(),
]

for (const node of sections) {
  if (node) app.appendChild(node)
}

// One shared scroll-reveal observer for every [.reveal] on the page.
const io = new IntersectionObserver(
  (entries) => {
    for (const e of entries) {
      if (e.isIntersecting) {
        e.target.classList.add('in-view')
        io.unobserve(e.target)
      }
    }
  },
  { rootMargin: '0px 0px -12% 0px', threshold: 0.08 },
)
document.querySelectorAll('.reveal').forEach((el) => io.observe(el))

// Active-section highlighting for the nav (progressive enhancement).
const navLinks = [...document.querySelectorAll('[data-navlink]')]
if (navLinks.length) {
  const spy = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue
        const id = e.target.id
        navLinks.forEach((l) => l.classList.toggle('is-active', l.getAttribute('href') === `#${id}`))
      }
    },
    { rootMargin: '-45% 0px -50% 0px' },
  )
  document.querySelectorAll('section[id]').forEach((s) => spy.observe(s))
}

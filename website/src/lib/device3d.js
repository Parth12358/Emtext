// A three.js render of the emtext pendant: the M5StickS3 form factor (48x24x15
// mm, R3 corners, a 1.14" 135x240 LCD, one accent-coloured front key) as it would ship as
// a product rather than a dev kit -- no header, no Grove socket, no pinout
// label; just the screen, the three keys, a USB-C for charging and the mic. Dimensions and key placements are from M5Stack's StickS3
// drawing, https://docs.m5stack.com/en/core/StickS3. Its screen is a live canvas
// texture that plays the firmware's GLANCE screen (firmware/emtext/src/display/
// display.cpp, drawGlance + drawTopBar + drawIndicators) over the site's demo
// examples: a 12 px status strip, the 16x16 Undertale heart coloured by tone,
// a bold five-word read, and the button-press bars along the bottom edge. The
// transcript is deliberately NOT on the glance -- the heart and the read are
// the whole screen, exactly as on the device.
//
// Everything is procedural geometry — no external model file, no image assets —
// so it stays self-contained and ships in the JS bundle. Falls back to a static
// DOM card if WebGL is unavailable.

import * as THREE from 'three'
import { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js'

// Real device proportions (mm) → scene units at 1u = 10mm.
const MM = 0.1
const W = 24 * MM
const H = 48 * MM
const D = 14 * MM

// Undertale soul colours, verbatim from display.cpp toneBar() (fw 0.2.0). One
// shape for every emotion; the emotion is the colour. Neutral is a dim gray
// heart. The key is the server's headline `emotion.label`, the voice model's
// seven labels; the older five tone names still resolve for old servers.
const TONE_RGB = {
  angry: 'rgb(255,0,40)',       // Determination
  surprised: 'rgb(255,140,0)',  // Bravery
  happy: 'rgb(255,216,0)',      // Justice
  disgusted: 'rgb(0,192,0)',    // Kindness
  fearful: 'rgb(66,255,255)',   // Patience
  sad: 'rgb(0,120,255)',        // Integrity
  neutral: 'rgb(120,120,120)',
  positive: 'rgb(255,0,40)',
  negative: 'rgb(0,120,255)',
  sarcastic: 'rgb(190,30,255)',
  mixed: 'rgb(255,216,0)',
}
// UI-chrome colours from display.cpp (cPos / cMis / cDim / cFaint, top-bar bg,
// resting indicator navy).
const C = {
  pos: 'rgb(90,170,90)',
  mis: 'rgb(220,160,40)',
  dim: 'rgb(150,150,150)',
  faint: 'rgb(90,90,90)',
  bar: 'rgb(12,22,28)',
  rest: 'rgb(2,8,46)',
}
// display.cpp HEART16: 16 rows, MSB = column 0.
const HEART16 = [
  0x300c, 0x7c3e, 0xfe7f, 0xfe7f, 0xffff, 0xffff, 0xffff, 0xffff,
  0xffff, 0xffff, 0x3ffc, 0x3ffc, 0x0ff0, 0x0ff0, 0x03c0, 0x03c0,
]

const easeOutCubic = (x) => 1 - Math.pow(1 - x, 3)

/**
 * Mount the 3D device into `container`.
 * @returns {{ dispose: () => void }}
 */
export function mountDevice(container, { examples, reduceMotion = false }) {
  // --- screen canvas (portrait 135:240) drawn at high-DPI for crispness ------
  const SCALE = 4
  const SW = 135 * SCALE
  const SH = 240 * SCALE
  const canvas = document.createElement('canvas')
  canvas.width = SW
  canvas.height = SH
  const ctx = canvas.getContext('2d')

  let webgl
  try {
    webgl = buildScene(container, canvas)
  } catch (err) {
    // No WebGL — degrade to a static card so the hero still communicates.
    return staticFallback(container, examples[0], ctx, canvas, drawScreen)
  }

  const { renderer, scene, camera, group, texture, resize, setVisible } = webgl

  // --- demo state machine (drives the screen canvas) -------------------------
  // Mirrors what the device does between reads: the glance keeps the LAST heart
  // up while listening, shows the amber "..." while the server thinks, then
  // repaints with the new tone and read. There is no typing phase because the
  // transcript never appears on the glance.
  const PHASE = { LISTENING: 0, THINKING: 1, READING: 2 }
  let idx = 0
  let phase = PHASE.LISTENING
  let t = 0 // seconds elapsed in current phase
  let dirty = true
  let blink = false // the listening dot on the status strip

  function current() {
    return examples[idx % examples.length]
  }
  function previous() {
    return examples[(idx + examples.length - 1) % examples.length]
  }

  // Tap / click: skip straight to the next line (the server has "heard" you).
  function next() {
    if (phase === PHASE.READING) {
      phase = PHASE.LISTENING
      idx++
    } else {
      phase = PHASE.THINKING
    }
    t = 0
    dirty = true
  }

  function step(dt) {
    t += dt
    const b = Math.floor(t * 3) % 2 === 0
    if (b !== blink && phase !== PHASE.READING) {
      blink = b
      dirty = true
    }
    if (phase === PHASE.LISTENING) {
      if (t > (reduceMotion ? 0.6 : 1.1)) {
        phase = PHASE.THINKING
        t = 0
        dirty = true
      }
    } else if (phase === PHASE.THINKING) {
      if (t > (reduceMotion ? 0.5 : 0.8)) {
        phase = PHASE.READING
        t = 0
        dirty = true
      }
    } else if (phase === PHASE.READING) {
      if (t > (reduceMotion ? 4.2 : 3.6)) {
        phase = PHASE.LISTENING
        t = 0
        idx++
        dirty = true
      }
    }
  }

  function render() {
    if (dirty) {
      // Before a read lands the glance still shows the previous heart, as the
      // real device does; the very first cycle starts from a neutral heart.
      const shown = phase === PHASE.READING ? current() : (idx === 0 ? null : previous())
      drawScreen(ctx, canvas, {
        ex: shown,
        listening: phase === PHASE.LISTENING && blink,
        thinking: phase === PHASE.THINKING,
      })
      texture.needsUpdate = true
      dirty = false
    }
  }

  // --- animation loop --------------------------------------------------------
  let raf = 0
  let running = true
  let last = 0
  const restTilt = -0.14
  const drag = { active: false, x: 0, y: 0, vx: 0, vy: 0, lx: 0, ly: 0, moved: 0 }
  const INTRO = 1.5 // seconds — the "float up and settle" intro
  let introT = reduceMotion ? 1 : 0

  function loop(now) {
    raf = requestAnimationFrame(loop)
    if (!running) return
    const dt = last ? Math.min((now - last) / 1000, 0.05) : 0
    last = now

    step(dt)
    render()

    const s = now / 1000
    const swayX = Math.sin(s * 0.6) * 0.03
    const swayY = Math.sin(s * 0.32) * 0.16
    const floatY = Math.sin(s * 0.8) * 0.04

    // The drag offset decays back to the resting sway once released, so the
    // device always drifts home and never gets stuck showing its back.
    if (!drag.active) {
      drag.vx *= 0.92
      drag.vy *= 0.92
      drag.x += drag.vx
      drag.y += drag.vy
      drag.x *= 0.96
      drag.y *= 0.96
    }

    if (reduceMotion) {
      group.rotation.set(restTilt + drag.y, -0.3 + drag.x, 0)
      group.scale.setScalar(1)
      group.position.y = 0
      renderer.domElement.style.opacity = '1'
    } else {
      if (introT < 1) introT = Math.min(1, introT + dt / INTRO)
      const e = easeOutCubic(introT)
      // Blend from an intro pose (small, low, turned away, faded) into the
      // resting gentle sway — Apple-style product reveal.
      group.rotation.set(
        -0.05 * (1 - e) + (restTilt + swayX) * e + drag.y,
        -1.15 * (1 - e) + (-0.3 + swayY) * e + drag.x,
        0,
      )
      group.position.y = -0.95 * (1 - e) + floatY * e
      group.scale.setScalar(0.7 + 0.3 * e)
      renderer.domElement.style.opacity = String(Math.min(1, e * 1.3))
    }
    renderer.render(scene, camera)
  }
  raf = requestAnimationFrame(loop)

  // --- pointer: drag to turn, tap for the next line ---------------------------
  const el = renderer.domElement
  el.style.cursor = 'grab'
  el.style.touchAction = 'pan-y'
  el.addEventListener('pointerdown', (e) => {
    drag.active = true
    drag.moved = 0
    drag.lx = e.clientX
    drag.ly = e.clientY
    drag.vx = drag.vy = 0
    el.setPointerCapture(e.pointerId)
    el.style.cursor = 'grabbing'
  })
  el.addEventListener('pointermove', (e) => {
    if (!drag.active) return
    const dx = e.clientX - drag.lx
    const dy = e.clientY - drag.ly
    drag.lx = e.clientX
    drag.ly = e.clientY
    drag.moved += Math.abs(dx) + Math.abs(dy)
    drag.vx = dx * 0.006
    drag.vy = dy * 0.004
    drag.x = Math.max(-1.3, Math.min(1.3, drag.x + drag.vx))
    drag.y = Math.max(-0.5, Math.min(0.5, drag.y + drag.vy))
  })
  const release = (e) => {
    if (!drag.active) return
    drag.active = false
    el.style.cursor = 'grab'
    if (drag.moved < 6) next() // a tap, not a drag
  }
  el.addEventListener('pointerup', release)
  el.addEventListener('pointercancel', release)
  el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      next()
    }
  })
  el.tabIndex = 0
  el.setAttribute('role', 'button')
  el.setAttribute('aria-label', 'The pendant. Drag to turn it; press to hear the next line.')

  // Pause when offscreen or tab hidden — no point spinning a hidden canvas.
  const io = new IntersectionObserver(
    (entries) => {
      running = entries[0].isIntersecting
      setVisible(running)
      if (running) last = 0
    },
    { threshold: 0.05 },
  )
  io.observe(container)

  const onVis = () => {
    running = !document.hidden
    if (running) last = 0
  }
  document.addEventListener('visibilitychange', onVis)

  const ro = new ResizeObserver(() => resize())
  ro.observe(container)

  return {
    dispose() {
      cancelAnimationFrame(raf)
      io.disconnect()
      ro.disconnect()
      document.removeEventListener('visibilitychange', onVis)
      renderer.dispose()
      texture.dispose()
      renderer.domElement.remove()
    },
  }
}

// ---------------------------------------------------------------------------
// Scene construction
// ---------------------------------------------------------------------------
function buildScene(container, canvas) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
  renderer.outputColorSpace = THREE.SRGBColorSpace
  const w = container.clientWidth || 480
  const h = container.clientHeight || 520
  renderer.setSize(w, h)
  container.appendChild(renderer.domElement)
  renderer.domElement.style.display = 'block'
  renderer.domElement.style.width = '100%'
  renderer.domElement.style.height = '100%'
  renderer.domElement.style.opacity = '0' // faded in by the intro tween

  const scene = new THREE.Scene()
  const camera = new THREE.PerspectiveCamera(30, w / h, 0.1, 100)
  // Shift the look-at down a touch: the content is bottom-heavy because the
  // 24 mm dimension sits below the device, so this centers the whole group.
  camera.position.set(0, -0.35, 11.9)

  const group = new THREE.Group()
  group.rotation.set(-0.14, -0.32, 0)
  scene.add(group)

  // --- the body ---------------------------------------------------------------
  // The product version of the StickS3 shell. Placements (mm from the top of
  // the device) come from M5Stack's outline drawing; everything a wearer never
  // touches -- the HAT header, Grove socket, screws, pinout label -- is gone,
  // leaving a plain shell.
  //   shell   48 x 24 x 15, R3 corners, warm ivory
  //   screen  window 16 x 26 at 5..31; LCD active ~15 x 25
  //   KEY1    the accent pill on the front, 12 x 2.5 at 37..39.5 (hold = record)
  //   KEY2    square key on the LEFT side at 18..24
  //   power   small key on the RIGHT side at 36..40
  //   USB-C   bottom end, centred (charging only)
  //   mic     a single hole on the front, bottom-left
  const mm = (n) => n * MM
  const fromTop = (n) => H / 2 - mm(n)

  // Warm ivory shell (the page's own paper tone, so the device sits in the
  // site rather than on it), a black screen plate, and the front key in the
  // site accent so the one control the wearer uses is the one thing in colour.
  const shellMat = new THREE.MeshPhysicalMaterial({
    color: new THREE.Color('#ece6da'),
    roughness: 0.5,
    metalness: 0.0,
    clearcoat: 0.35,
    clearcoatRoughness: 0.35,
  })
  const darkMat = new THREE.MeshStandardMaterial({ color: new THREE.Color('#1a1b1e'), roughness: 0.8 })
  const keyMat = new THREE.MeshPhysicalMaterial({
    color: new THREE.Color('#e3ddd0'), roughness: 0.5, clearcoat: 0.3, clearcoatRoughness: 0.35,
  })

  const body = new THREE.Mesh(new RoundedBoxGeometry(W, H, D, 18, mm(3)), shellMat)
  group.add(body)

  // Screen window: a recess with a black floor, the LCD (canvas texture), and
  // a faint cover-glass sheen.
  const winW = mm(16)
  const winH = mm(26)
  const winY = fromTop(18)
  const window_ = new THREE.Mesh(new RoundedBoxGeometry(winW, winH, 0.06, 6, 0.06), darkMat)
  window_.position.set(0, winY, D / 2 - 0.012)
  group.add(window_)

  const screenW = mm(14.9)
  const screenH = mm(24.9)
  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  texture.anisotropy = renderer.capabilities.getMaxAnisotropy()
  const screen = new THREE.Mesh(
    new THREE.PlaneGeometry(screenW, screenH),
    new THREE.MeshBasicMaterial({ map: texture, toneMapped: false }),
  )
  screen.position.set(0, winY, D / 2 + 0.022)
  group.add(screen)
  const sheen = new THREE.Mesh(
    new THREE.PlaneGeometry(winW, winH),
    new THREE.MeshPhysicalMaterial({
      color: new THREE.Color('#ffffff'), transparent: true, opacity: 0.06,
      roughness: 0.05, metalness: 0.4, clearcoat: 1, depthWrite: false,
    }),
  )
  sheen.position.set(0, winY, D / 2 + 0.024)
  group.add(sheen)

  // KEY1: the accent-coloured pill below the screen. On the pendant this is the one key
  // the wearer uses: hold it to save the moment as a clip.
  const accent = new THREE.MeshPhysicalMaterial({
    color: new THREE.Color('#ff4d00'), roughness: 0.32, clearcoat: 0.9, clearcoatRoughness: 0.15,
  })
  const key1 = new THREE.Mesh(new RoundedBoxGeometry(mm(12), mm(2.6), 0.07, 8, mm(1.25)), accent)
  key1.position.set(0, fromTop(38.3), D / 2 + 0.03)
  group.add(key1)
  const key1Well = new THREE.Mesh(new RoundedBoxGeometry(mm(13), mm(3.6), 0.02, 8, mm(1.75)), new THREE.MeshStandardMaterial({ color: new THREE.Color("#d6cfc1"), roughness: 0.7 }))  // a soft shadow ring, not a black cut-out
  key1Well.position.set(0, fromTop(38.3), D / 2 + 0.006)
  group.add(key1Well)

  // Side keys: KEY2 on the left at mid-height, power on the right near the
  // bottom, both shell-coloured and only just proud.
  const key2 = new THREE.Mesh(new RoundedBoxGeometry(0.06, mm(6), mm(6), 6, 0.05), keyMat)
  key2.position.set(-W / 2 - 0.012, fromTop(21), 0.06)
  group.add(key2)
  const pwr = new THREE.Mesh(new RoundedBoxGeometry(0.06, mm(4), mm(4.5), 6, 0.05), keyMat)
  pwr.position.set(W / 2 + 0.012, fromTop(38), 0.06)
  group.add(pwr)

  // Mic hole, front bottom-left.
  const mic = new THREE.Mesh(new THREE.CircleGeometry(mm(0.6), 16), new THREE.MeshBasicMaterial({ color: 0x000000 }))
  mic.position.set(-mm(8), fromTop(44), D / 2 + 0.003)
  group.add(mic)

  // USB-C, centred on the bottom end: the only port a product needs.
  const usbRim = new THREE.Mesh(
    new RoundedBoxGeometry(mm(8.9), 0.04, mm(3.2), 6, mm(1.5)),
    new THREE.MeshStandardMaterial({ color: new THREE.Color('#9a9da6'), roughness: 0.35, metalness: 0.9 }),
  )
  usbRim.position.set(0, -H / 2 - 0.006, 0)
  group.add(usbRim)
  const usbCavity = new THREE.Mesh(new RoundedBoxGeometry(mm(8.2), 0.06, mm(2.5), 6, mm(1.2)), darkMat)
  usbCavity.position.set(0, -H / 2 - 0.004, 0)
  group.add(usbCavity)


  group.add(buildDimensions())

  // Lighting — a warm sky/ground pair for soft plastic shading, a warm key
  // from upper-left so the clearcoat picks up one clean highlight, a cool
  // fill from the right, and a rim from behind to separate the shell from
  // the page.
  scene.add(new THREE.HemisphereLight(0xfff6ea, 0xd9d3c6, 1.1))
  // A warm key from upper-left, a cool fill from the right and a rim from
  // behind, so the pale shell keeps its edges against the pale page.
  const key = new THREE.DirectionalLight(0xfff1e0, 1.7)
  key.position.set(-3.5, 5, 6)
  scene.add(key)
  const fill = new THREE.DirectionalLight(0xdfe8ff, 0.7)
  fill.position.set(4, -2, 4)
  scene.add(fill)
  const back = new THREE.DirectionalLight(0xffffff, 1.1)
  back.position.set(2, 3, -5)
  scene.add(back)

  function resize() {
    const cw = container.clientWidth || w
    const ch = container.clientHeight || h
    renderer.setSize(cw, ch)
    camera.aspect = cw / ch
    camera.updateProjectionMatrix()
  }

  function setVisible(v) {
    renderer.domElement.style.visibility = v ? 'visible' : 'visible'
  }

  return { renderer, scene, camera, group, texture, resize, setVisible }
}

// ---------------------------------------------------------------------------
// Dimension callouts (scale reference)
// ---------------------------------------------------------------------------
function buildDimensions() {
  const g = new THREE.Group()
  const mat = new THREE.LineBasicMaterial({ color: new THREE.Color('#8f8b80'), transparent: true, opacity: 0.85 })
  const gap = 0.34
  const tick = 0.12

  // vertical dimension on the right — the 48 mm height
  const rx = W / 2 + gap
  const vLine = new THREE.LineSegments(
    new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(rx, H / 2, 0), new THREE.Vector3(rx, -H / 2, 0),
      new THREE.Vector3(rx - tick, H / 2, 0), new THREE.Vector3(rx + tick, H / 2, 0),
      new THREE.Vector3(rx - tick, -H / 2, 0), new THREE.Vector3(rx + tick, -H / 2, 0),
    ]),
    mat,
  )
  g.add(vLine)
  const l48 = makeLabel('48 mm')
  l48.position.set(rx + 0.52, 0, 0)
  g.add(l48)

  // horizontal dimension along the bottom — the 24 mm width
  const by = -H / 2 - gap
  const hLine = new THREE.LineSegments(
    new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(-W / 2, by, 0), new THREE.Vector3(W / 2, by, 0),
      new THREE.Vector3(-W / 2, by - tick, 0), new THREE.Vector3(-W / 2, by + tick, 0),
      new THREE.Vector3(W / 2, by - tick, 0), new THREE.Vector3(W / 2, by + tick, 0),
    ]),
    mat,
  )
  g.add(hLine)
  const l24 = makeLabel('24 mm')
  l24.position.set(0, by - 0.3, 0)
  g.add(l24)

  return g
}

// A camera-facing text label (sprite) for a dimension.
function makeLabel(text) {
  const scale = 4
  const c = document.createElement('canvas')
  c.width = 128 * scale
  c.height = 40 * scale
  const cx = c.getContext('2d')
  cx.clearRect(0, 0, c.width, c.height)
  cx.font = `500 ${22 * scale}px "JetBrains Mono Variable", "JetBrains Mono", monospace`
  cx.fillStyle = '#57544c'
  cx.textAlign = 'center'
  cx.textBaseline = 'middle'
  cx.fillText(text, c.width / 2, c.height / 2)
  const tex = new THREE.CanvasTexture(c)
  tex.colorSpace = THREE.SRGBColorSpace
  const spr = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true }))
  spr.scale.set(1.15, 1.15 * (40 / 128), 1)
  return spr
}

// ---------------------------------------------------------------------------
// Screen drawing: the firmware glance, in portrait, at 135x240 design units.
// Positions are the ones in display.cpp so the render matches the device.
// ---------------------------------------------------------------------------
export function drawScreen(ctx, canvas, { ex, listening, thinking }) {
  const W = canvas.width
  const H = canvas.height
  const S = W / 135 // design px -> canvas px
  const px = (n) => Math.round(n * S)
  const STRIP = 12 // top bar and indicator bar thickness (display.cpp STRIP)
  const mono = '"JetBrains Mono Variable", "JetBrains Mono", monospace'
  const sans = '"Inter Variable", "Inter", "Helvetica Neue", Arial, sans-serif'

  ctx.fillStyle = '#000'
  ctx.fillRect(0, 0, W, H)

  // --- top bar (drawTopBar, portrait) ---------------------------------------
  ctx.fillStyle = C.bar
  ctx.fillRect(0, 0, W, px(STRIP))
  const cy = px(STRIP / 2)
  // connectivity glyph: filled disc = ready (colour is only the second cue)
  ctx.fillStyle = C.pos
  ctx.beginPath()
  ctx.arc(px(6), cy, px(3), 0, Math.PI * 2)
  ctx.fill()
  // live ping, left of centre; battery %, right. Font0 is a 6x8 pixel font.
  ctx.font = `500 ${px(7.5)}px ${mono}`
  ctx.textBaseline = 'middle'
  ctx.fillStyle = C.dim
  ctx.textAlign = 'left'
  ctx.fillText('41ms', px(14), cy)
  ctx.textAlign = 'right'
  ctx.fillText('87%', W - px(2), cy)
  // dev activity cluster, centre: the listening dot while the mic hears voice
  if (listening) {
    ctx.fillStyle = '#fff'
    ctx.beginPath()
    ctx.arc(px(135 / 2 - 14), cy, px(2), 0, Math.PI * 2)
    ctx.fill()
  }

  // --- the heart (drawHeart at W/2, 69; 16 cells of 6 px) --------------------
  const tone = ex ? (ex.emotion || ex.tone) : 'neutral'
  const col = TONE_RGB[tone] || TONE_RGB.neutral
  const cell = 6
  const ox = 135 / 2 - 8 * cell
  const oy = 69 - 8 * cell
  ctx.fillStyle = col
  for (let r = 0; r < 16; r++) {
    const bits = HEART16[r]
    for (let c = 0; c < 16; c++) {
      if (bits & (0x8000 >> c)) ctx.fillRect(px(ox + c * cell), px(oy + r * cell), px(cell), px(cell))
    }
  }

  // --- the read (FreeSansBold9pt, white; faint when the server was not sure) -
  if (ex) {
    const text = capWords(ex.glance || ex.read, 5)
    ctx.font = `700 ${px(12.5)}px ${sans}`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'top'
    ctx.fillStyle = ex.conf === 'low' ? C.faint : '#fff'
    const lh = px(17)
    const lines = wrapLines(ctx, text, W - px(6), 5)
    const y0 = px(172) - (lines.length * lh) / 2
    lines.forEach((l, k) => ctx.fillText(l, W / 2, y0 + k * lh))
  }

  // --- "..." while a read is pending (amber, clear of the top bar) ----------
  if (thinking) {
    ctx.font = `600 ${px(9)}px ${mono}`
    ctx.textAlign = 'left'
    ctx.textBaseline = 'top'
    ctx.fillStyle = C.mis
    ctx.fillText('...', px(14), px(13))
  }

  // --- button-press indicators (drawIndicators, portrait "U") ----------------
  ctx.fillStyle = C.rest
  ctx.fillRect(0, H - px(35), px(STRIP), px(35))                          // PWR, left
  ctx.fillRect(W - px(STRIP), H - px(35), px(STRIP), px(35))              // B, right
  ctx.fillRect(px(STRIP), H - px(STRIP), W - 2 * px(STRIP), px(STRIP))   // A, bottom
}

// display.cpp capWords: keep the first `max` words, append "..." if more.
function capWords(s, max) {
  const w = s.trim().split(/\s+/)
  return w.length <= max ? w.join(' ') : w.slice(0, max).join(' ') + '...'
}

// Greedy word wrap into at most `maxLines` lines (display.cpp wrapN).
function wrapLines(ctx, text, maxW, maxLines) {
  const words = text.split(' ')
  const lines = []
  let line = ''
  for (const w of words) {
    const test = line ? line + ' ' + w : w
    if (ctx.measureText(test).width > maxW && line) {
      lines.push(line)
      line = w
    } else {
      line = test
    }
    if (lines.length === maxLines) break
  }
  if (line && lines.length < maxLines) lines.push(line)
  return lines
}

// ---------------------------------------------------------------------------
// Fallback (no WebGL): a static screen card so the hero still says something.
// ---------------------------------------------------------------------------
function staticFallback(container, ex, ctx, canvas, draw) {
  draw(ctx, canvas, { ex, listening: false, thinking: false })
  const img = document.createElement('img')
  img.src = canvas.toDataURL()
  img.alt = 'emtext pendant glance: a pixel heart coloured by tone above a short read'
  img.style.cssText = 'width:min(60%,240px);border-radius:14px;box-shadow:var(--shadow-2);display:block;margin:auto;'
  container.appendChild(img)
  return { dispose() { img.remove() } }
}

// A three.js render of the M5StickS3 pendant (the real hardware C-ontext runs on:
// 48x24x15 mm, a 1.14" 135x240 LCD, orange body). Its screen is a live canvas
// texture that plays the same tone-read demo the site is built around.
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

const TONE_HEX = {
  positive: '#1f8f4d',
  negative: '#e0301e',
  neutral: '#6f6c61',
  sarcastic: '#7a45c9',
  mixed: '#b26a00',
}

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
  const CHAR_S = 0.026 // seconds per character while "typing"
  const PHASE = { RESET: 0, TYPING: 1, THINKING: 2, READING: 3 }
  let idx = 0
  let phase = PHASE.RESET
  let t = 0 // seconds elapsed in current phase
  let typed = 0
  let dirty = true

  function current() {
    return examples[idx % examples.length]
  }

  function step(dt) {
    t += dt
    const ex = current()
    if (phase === PHASE.RESET) {
      if (t > (reduceMotion ? 0.25 : 0.5)) {
        phase = PHASE.TYPING
        t = 0
        typed = reduceMotion ? ex.text.length : 0
        dirty = true
      }
    } else if (phase === PHASE.TYPING) {
      if (reduceMotion) {
        phase = PHASE.THINKING
        t = 0
      } else {
        const want = Math.min(ex.text.length, Math.floor(t / CHAR_S))
        if (want !== typed) {
          typed = want
          dirty = true
        }
        if (typed >= ex.text.length) {
          phase = PHASE.THINKING
          t = 0
        }
      }
    } else if (phase === PHASE.THINKING) {
      if (t > (reduceMotion ? 0.5 : 0.75)) {
        phase = PHASE.READING
        t = 0
        dirty = true
      }
    } else if (phase === PHASE.READING) {
      if (t > (reduceMotion ? 3.8 : 3.3)) {
        phase = PHASE.RESET
        t = 0
        idx++
        dirty = true
      }
    }
  }

  function render() {
    if (dirty) {
      drawScreen(ctx, canvas, {
        ex: current(),
        typed,
        stateLabel:
          phase === PHASE.RESET ? 'listening'
          : phase === PHASE.TYPING ? 'heard'
          : phase === PHASE.THINKING ? 'thinking'
          : 'read',
        showRead: phase === PHASE.READING,
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

    if (reduceMotion) {
      group.rotation.set(restTilt, -0.3, 0)
      group.scale.setScalar(1)
      group.position.y = 0
      renderer.domElement.style.opacity = '1'
    } else {
      if (introT < 1) introT = Math.min(1, introT + dt / INTRO)
      const e = easeOutCubic(introT)
      // Blend from an intro pose (small, low, turned away, faded) into the
      // resting gentle sway — Apple-style product reveal.
      group.rotation.set(
        -0.05 * (1 - e) + (restTilt + swayX) * e,
        -1.15 * (1 - e) + (-0.3 + swayY) * e,
        0,
      )
      group.position.y = -0.95 * (1 - e) + floatY * e
      group.scale.setScalar(0.7 + 0.3 * e)
      renderer.domElement.style.opacity = String(Math.min(1, e * 1.3))
    }
    renderer.render(scene, camera)
  }
  raf = requestAnimationFrame(loop)

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

  // Body — orange rounded stick.
  const bodyMat = new THREE.MeshStandardMaterial({
    color: new THREE.Color('#f24d0d'),
    roughness: 0.42,
    metalness: 0.05,
  })
  const body = new THREE.Mesh(new RoundedBoxGeometry(W, H, D, 6, 0.3), bodyMat)
  group.add(body)

  // Black front faceplate, slightly proud of the orange body.
  const faceMat = new THREE.MeshStandardMaterial({
    color: new THREE.Color('#141519'),
    roughness: 0.5,
    metalness: 0.1,
  })
  const face = new THREE.Mesh(new RoundedBoxGeometry(W * 0.92, H * 0.95, 0.06, 5, 0.16), faceMat)
  face.position.set(0, 0, D / 2 - 0.0)
  group.add(face)

  // Screen bezel (dark) + the emissive LCD showing the canvas texture.
  // Real device: a 1.14" 135x240 panel (~14x25mm) set in the UPPER portion of
  // the 24x48mm face, with a wide black bezel around it.
  const screenW = W * 0.58
  const screenH = screenW * (240 / 135)
  const screenY = H * 0.16
  const bezel = new THREE.Mesh(
    new THREE.PlaneGeometry(screenW + 0.13, screenH + 0.13),
    new THREE.MeshBasicMaterial({ color: new THREE.Color('#050609') }),
  )
  bezel.position.set(0, screenY, D / 2 + 0.031)
  group.add(bezel)

  const texture = new THREE.CanvasTexture(canvas)
  texture.colorSpace = THREE.SRGBColorSpace
  texture.anisotropy = renderer.capabilities.getMaxAnisotropy()
  const screen = new THREE.Mesh(
    new THREE.PlaneGeometry(screenW, screenH),
    new THREE.MeshBasicMaterial({ map: texture, toneMapped: false }),
  )
  screen.position.set(0, screenY, D / 2 + 0.034)
  group.add(screen)

  // Front button — a small horizontal pill below the screen (BtnA), not a
  // round home button. Matches the StickS3 front face.
  const btnMat = new THREE.MeshStandardMaterial({ color: new THREE.Color('#24262c'), roughness: 0.62 })
  const btn = new THREE.Mesh(new RoundedBoxGeometry(W * 0.34, 0.2, 0.08, 4, 0.09), btnMat)
  btn.position.set(0, -H * 0.24, D / 2 + 0.028)
  group.add(btn)

  // Tiny status LED just under the screen.
  const led = new THREE.Mesh(
    new THREE.CircleGeometry(0.025, 20),
    new THREE.MeshBasicMaterial({ color: new THREE.Color('#8fe36b') }),
  )
  led.position.set(W * 0.3, -H * 0.24, D / 2 + 0.033)
  group.add(led)

  // Side power button (red) on the left edge, upper-middle.
  const sideBtn = new THREE.Mesh(
    new THREE.BoxGeometry(0.05, 0.3, 0.3),
    new THREE.MeshStandardMaterial({ color: new THREE.Color('#c62b1c'), roughness: 0.5 }),
  )
  sideBtn.position.set(-W / 2 - 0.015, H * 0.1, 0)
  group.add(sideBtn)

  // USB-C port on the top edge.
  const portMat = new THREE.MeshStandardMaterial({ color: new THREE.Color('#0a0b0e'), roughness: 0.6, metalness: 0.4 })
  const usbc = new THREE.Mesh(new RoundedBoxGeometry(W * 0.34, 0.16, 0.3, 3, 0.07), portMat)
  usbc.position.set(0, H / 2 - 0.01, 0)
  group.add(usbc)

  // Bottom pin-header block (the HAT connector).
  const header = new THREE.Mesh(
    new THREE.BoxGeometry(W * 0.62, 0.16, 0.34),
    new THREE.MeshStandardMaterial({ color: new THREE.Color('#101114'), roughness: 0.85 }),
  )
  header.position.set(0, -H / 2 + 0.03, 0)
  group.add(header)

  // Dimension callouts — a scale reference, so you can tell it's a 48x24mm
  // pendant, not a phone. Technical-drawing style: thin leader lines with end
  // ticks and camera-facing labels. Added to the group so they frame the
  // device as it turns.
  group.add(buildDimensions())

  // Lighting — warm key, cool fill, rim to catch the rounded edges.
  scene.add(new THREE.AmbientLight(0xffffff, 0.72))
  const key = new THREE.DirectionalLight(0xfff3e6, 1.5)
  key.position.set(-3, 5, 6)
  scene.add(key)
  const fill = new THREE.DirectionalLight(0xdfe6ff, 0.5)
  fill.position.set(4, -1, 4)
  scene.add(fill)
  const rim = new THREE.DirectionalLight(0xffffff, 0.8)
  rim.position.set(2, 3, -5)
  scene.add(rim)

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
// Screen drawing (the live tone-read demo, rendered to the canvas texture)
// ---------------------------------------------------------------------------
function drawScreen(ctx, canvas, { ex, typed, stateLabel, showRead }) {
  const W = canvas.width
  const H = canvas.height
  const S = W / 135 // scale factor from design units

  // background
  const g = ctx.createLinearGradient(0, 0, 0, H)
  g.addColorStop(0, '#0b0e14')
  g.addColorStop(1, '#05060a')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, W, H)

  const pad = 9 * S
  const mono = '"JetBrains Mono Variable", "JetBrains Mono", monospace'
  const serif = '"Fraunces Variable", "Fraunces", Georgia, serif'

  // header: mic → server   ·   state
  ctx.textBaseline = 'alphabetic'
  ctx.font = `500 ${6.5 * S}px ${mono}`
  ctx.fillStyle = '#7b8394'
  ctx.textAlign = 'left'
  ctx.fillText('mic → server', pad, pad + 7 * S)
  ctx.textAlign = 'right'
  ctx.fillStyle = showRead ? '#ff6a2b' : '#7b8394'
  ctx.fillText(stateLabel, W - pad, pad + 7 * S)

  // divider
  ctx.strokeStyle = 'rgba(255,255,255,0.10)'
  ctx.lineWidth = Math.max(1, S)
  ctx.beginPath()
  ctx.moveTo(pad, pad + 13 * S)
  ctx.lineTo(W - pad, pad + 13 * S)
  ctx.stroke()

  // transcript (mono, wrapped), with a typing caret
  const shown = ex.text.slice(0, typed)
  ctx.font = `500 ${8.6 * S}px ${mono}`
  ctx.fillStyle = '#eef1f6'
  ctx.textAlign = 'left'
  const lh = 12 * S
  let y = pad + 27 * S
  const maxW = W - pad * 2
  const caretY = drawWrapped(ctx, shown, pad, y, maxW, lh)
  if (typed < ex.text.length) {
    // blinking-ish caret block at the end
    ctx.fillStyle = '#ff6a2b'
    ctx.fillRect(caretY.x + 1 * S, caretY.y - 8 * S, 4 * S, 10 * S)
  }

  if (!showRead) return

  // read block: tone rule + chip + voice + serif read text
  const tone = ex.tone
  const hex = TONE_HEX[tone] || '#9aa'
  const readTop = H * 0.52
  ctx.strokeStyle = hex
  ctx.lineWidth = 2 * S
  ctx.beginPath()
  ctx.moveTo(pad, readTop)
  ctx.lineTo(pad, H - pad)
  ctx.stroke()

  const cx = pad + 6 * S
  // tone chip
  ctx.font = `600 ${6.5 * S}px ${mono}`
  const chipText = tone
  const chipW = ctx.measureText(chipText).width + 14 * S
  const chipH = 12 * S
  const chipY = readTop + 2 * S
  roundRect(ctx, cx, chipY, chipW, chipH, 6 * S)
  ctx.fillStyle = hexA(hex, 0.18)
  ctx.fill()
  ctx.strokeStyle = hexA(hex, 0.55)
  ctx.lineWidth = Math.max(1, S)
  ctx.stroke()
  ctx.fillStyle = hex
  ctx.textBaseline = 'middle'
  ctx.fillText(chipText, cx + 7 * S, chipY + chipH / 2 + 0.5 * S)
  ctx.textBaseline = 'alphabetic'

  // voice line
  ctx.font = `500 ${6 * S}px ${mono}`
  ctx.fillStyle = '#8b93a1'
  ctx.fillText(`voice: ${ex.voice}`, cx, chipY + chipH + 11 * S)

  // read text (serif)
  ctx.font = `450 ${9.6 * S}px ${serif}`
  ctx.fillStyle = '#f3f5f9'
  drawWrapped(ctx, ex.read, cx, chipY + chipH + 26 * S, maxW - 6 * S, 11.5 * S)
}

function drawWrapped(ctx, text, x, y, maxW, lh) {
  const words = text.split(' ')
  let line = ''
  let cx = x
  let cy = y
  for (let i = 0; i < words.length; i++) {
    const test = line ? line + ' ' + words[i] : words[i]
    if (ctx.measureText(test).width > maxW && line) {
      ctx.fillText(line, x, cy)
      line = words[i]
      cy += lh
    } else {
      line = test
    }
  }
  if (line) {
    ctx.fillText(line, x, cy)
    cx = x + ctx.measureText(line).width
  }
  return { x: cx, y: cy }
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath()
  ctx.moveTo(x + r, y)
  ctx.arcTo(x + w, y, x + w, y + h, r)
  ctx.arcTo(x + w, y + h, x, y + h, r)
  ctx.arcTo(x, y + h, x, y, r)
  ctx.arcTo(x, y, x + w, y, r)
  ctx.closePath()
}

function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16)
  const r = (n >> 16) & 255
  const g = (n >> 8) & 255
  const b = n & 255
  return `rgba(${r},${g},${b},${a})`
}

// ---------------------------------------------------------------------------
// Fallback (no WebGL): a static screen card so the hero still says something.
// ---------------------------------------------------------------------------
function staticFallback(container, ex, ctx, canvas, draw) {
  draw(ctx, canvas, { ex, typed: ex.text.length, stateLabel: 'read', showRead: true })
  const img = document.createElement('img')
  img.src = canvas.toDataURL()
  img.alt = 'C-ontext pendant screen showing a live tone read'
  img.style.cssText = 'width:min(60%,240px);border-radius:14px;box-shadow:var(--shadow-2);display:block;margin:auto;'
  container.appendChild(img)
  return { dispose() { img.remove() } }
}

# emtext pendant — UI/UX reference & plan

A complete walkthrough of the device's on-screen UI and physical interactions: every screen,
what it looks like, how you get in and out, what every button/gesture does, and every transition
between them. It is **both** a record of what's built **and** the target design we're building
toward — so each screen is split into what exists today vs. where it's going.

**Source of truth for "Now":** the actual code — `src/display/display.cpp` (screens),
`src/controls/controls.cpp` (button/IMU events), `emtext.ino` (wiring), `src/portal/portal.cpp`
(portal). If the doc and the code disagree about *Now*, the code wins — fix the doc.

**Legend:** ✅ built & on-device · 🔲 target / planned, not built yet · ⚠️ gap / rough edge.
Where the code name differs from the new UX name, both are given (e.g. *More Info* is
`display::State::History` in code).

---

## 1. UX principles

- **The emotion read is the hero.** The glance leads with the emotional tone (symbol + color),
  not text. Words are supporting context, not the headline.
- **Desaturated palette = observation, not alarm.** Muted tones read as a calm note, not an alert.
- **Redundant encoding — never color alone.** Tone is carried by shape *and* color, so it
  survives color-blindness and a dim screen.
- **Neutral = quiet.** When there's nothing worth flagging, the UI stays still.
- **Static frames, no scrolling text / no constant animation** — glanceable and power-cheap.
  (Animations are an explicit *nice-to-have*, not a default.)
- **Privacy is unmistakable.** Pause is a distinct screen that hard-stops the mic.
- **Chrome stays out of the way.** Persistent info (battery, connectivity, clock) lives in a
  small info bar / corner, never competing with the semantic read.

---

## 2. Screen map

Four screens + a **Paused** privacy overlay (a flag drawn ahead of everything) + persistent
chrome overlays (info bar, clock, button-press shadows).

| Screen | Code name | Now | Purpose |
|---|---|---|---|
| **Boot / Quick Setup** | splash | ✅ splash · 🔲 quick setup | logo + connect progress; offer setup while connecting |
| **Dark** | `State::Dark` | ✅ | resting/idle; screen black; default state |
| **Glance** | `State::Glance` | ✅ | the live read: tone symbol + LLM context + chrome |
| **More Info** | `State::History` | ✅ (as history) | longer LLM context / summary of recent reads |
| **Settings** | `State::Status` | ✅ scrollable page | rows: wifi/bright/power + portal-lock panel |
| **Paused** (overlay) | `g_paused` flag | ✅ | privacy screen; mic hard-off |

**Chrome overlays** (target — ride on top of the live screens):

| Overlay | Now | Target |
|---|---|---|
| **Top bar** (connectivity + ping · battery) | ✅ `drawTopBar` — dark strip = connectivity/ping (+ a green/amber dot), magenta = battery. Top in portrait, left strip in landscape | richer connectivity/ping + battery-level fill |
| **Press indicators** (A/B/PWR) | ✅ `drawIndicators` — three edge segments, rest `#0D405F`, light on press (A red / B white / PWR green), on the physical-button edge, orientation-aware | — |
| **Clock** | 🔲 | small tasteful face, on the side/out of the way; larger than the summary text, **smaller** than the semantic read |
| **Processing "…"** | ✅ | shown while a read is pending (`status: thinking`) |

Both the top bar and the press indicators render on **every lit screen except Dark** (Paused stays clean). Screen content (titles, read, footer) was nudged inward to leave room for them.

---

## 3. Control reference

`M5.update()` runs first in `loop()`, then `controls::loop()` maps raw buttons/IMU to semantic
callbacks; `emtext.ino` decides what each does — **context-dependent on the current screen**.

| Input | Action (by screen) | Wired |
|---|---|---|
| **BtnA** click | Dark→Glance · Glance→More Info · More Info→Glance · **Settings→select row** | `onWake` ✅ |
| **BtnA** hold | Settings→back to Glance · elsewhere→**save a clip** (stub, needs server) | `onMute` ✅ (clip 🔲) |
| **BtnB** click | **Settings→scroll to next row** · elsewhere→privacy pause | `onPause` ✅ |
| **BtnB** hold | live screen→open Settings · **Settings→back to Glance** | `onStatus` ✅ |
| **BtnPWR** hold | → Dark (two-step power-off is Stage 8) | `onPowerOff` ✅ (interim) |
| **IMU** tilt | auto-rotate | `onOrient` ✅ |
| **IMU** lift | Dark → Glance (lift-to-wake) | `onLift` ✅ |

While the **setup portal is up**, Settings nav is **locked**: BtnB scroll and both back gestures
are dead; only BtnA (turn portal off) or PWR (→Dark) get you out.

**Timings & thresholds (Now):** Glance auto-dims to Dark after **8 s** (`GLANCE_MS`, reset on
each read). Click vs hold = M5Unified `wasClicked()`/`wasHold()` (~500 ms). IMU ~**20 Hz**;
orientation is **debounced** (`ORIENT_STABLE_MS` = **500 ms**) — a new quadrant must hold steady
that long before it flips, so it can't flicker near the 45° boundary. Lift = accel > **1.35 g** then a **1.5 s** cooldown
(`LIFT_G`, `LIFT_COOLDOWN_MS`, tune on hardware).

---

## 4. Per-screen walkthrough

### 4.1 Boot / Quick Setup — ⚠️ needs work (1/10)
- **Now ✅:** "emtext" centered, size 2, green on a real M5StickS3 / red otherwise (board sanity
  check), 600 ms, then `display::begin()` sets **Dark**. It's a dumb fixed delay — shows nothing
  about connection progress.
- **Target 🔲:** show connect progress (network → server → ready, + per-stage failure), and
  **offer Quick Setup** while it works toward connecting — i.e. a path into the setup portal
  right from boot instead of only from deep in Settings, so a first-run/unconfigured device is
  obviously configurable.

### 4.2 Dark ✅
- **On screen:** nothing — brightness 0, black. The normal resting state; everything defaults
  back here.
- **Controls from here:**
  - **BtnA click** → Glance.
  - **Lift gesture** → Glance.
  - **BtnB click** → Paused (works even from Dark).
  - **BtnB hold** → Settings.
  - **BtnA hold** → 🔲 **record a clip to save** (saves the current moment to the server, §9.7).
- **Transitions out:** Glance (BtnA/lift), Settings (BtnB-hold), Paused (BtnB click),
  clip capture (BtnA hold, 🔲).
- ⚠️ **Read while Dark doesn't wake the screen** — reads accumulate silently (open question §10).
- ⚠️ **No "mic live" affordance while Dark** — capture state should be externally visible
  (compliance).

### 4.3 Glance — the hero screen
**On-screen info (target):**
- **Emotional tone** as a symbol (the hero). ✅ built as the Undertale heart (`drawHeart`),
  colored by tone with Undertale soul colours (§6).
- **Context from the LLM** (the read). ✅ built as a small caption.
- **Connectivity** to server + wifi. ✅ conn dot; 🔲 richer info bar.
- **Battery level.** 🔲 (via info bar).
- **Clock / time.** 🔲
- **iOS-style button shadows** to hint BtnB/PWR. 🔲

**Nice-to-haves 🔲:** animations · wifi range/ping detail · battery level with uptime-remaining.

- **How you get here:** Dark→BtnA/lift; unpause; a new read while already on Glance refreshes it
  in place.
- **Controls from here:**
  - **BtnA click** → **More Info** (renamed from History).
  - **BtnB hold** → Settings.
  - **BtnB click** → Paused.
  - **BtnA hold** → 🔲 record a clip.
- **Transitions out:** More Info (BtnA), Settings (BtnB-hold), Paused (BtnB), **Dark after 8 s**
  of no new read.
- ⚠️ **Transcript is captured but not shown** (`g_lastTranscript` → `setGlance`, never rendered).
  Decide: surface it in More Info, or drop it.

### 4.4 More Info — `State::History` ✅ (being repurposed)
- **Now ✅:** "history" title + up to 5 recent reads (tone tick + ordinal label + read text),
  most-recent-first; "(nothing yet)" when empty. Rows aren't width-clipped ⚠️.
- **Target 🔲:** less of a 5-row list, more of a **detail page** — longer LLM context and a
  summary of the last few prompts/reads. The place you go when the glance caption isn't enough.
- **Controls from here:**
  - **BtnA click** → Glance.
  - **BtnB hold** → Settings.
  - **BtnB click** → Paused.
  - **BtnA hold** → 🔲 (clip, TBD in this context).
- ⚠️ **Does not auto-dim** — only Glance times out.

### 4.5 Settings — `State::Status` ✅ (scrollable settings page — built, pending hardware verify)
A scrollable page (`display.cpp:drawSettings()`), 3 rows with a `>` cursor, a `[B]next [A]sel`
hint top-right, and a faint footer aside (`ready | up Ns | NN%`). **Navigation:**
- **BtnB click** → scroll cursor to next row (wraps).
- **BtnA click** → select/activate the highlighted row.
- **BtnB hold** or **BtnA hold** → back to Glance · **PWR** → Dark.

**Rows ✅:**

| Row (code `Setting`) | Value shown | BtnA select does |
|---|---|---|
| **wifi** (`Wifi`) | joined SSID (trunc.) | launch/stop the setup **portal** |
| **bright** (`Brightness`) | `n/4` | cycle 4 brightness presets (`40/90/150/220`), applied live to all lit screens |
| **power** (`Power`) | battery `%` | ⚠️ stub — logs only (real power profiles = Stage 8) |

*(The **cues**/mute row is removed — the device has no audio output, so there is nothing to mute.)*

Dropped from the page per the plan: **URL/host** and **token** (secret) aren't shown; **ping**
isn't surfaced yet (net doesn't measure RTT — future). Uptime/battery live in the footer aside.

**Portal lock ✅:** while the setup AP is up, the page replaces the rows with a credentials
panel — **`join: <ssid>` · `pass: <apPass>` · `at: 192.168.4.1`** — and **freezes navigation**
(scroll + both back gestures dead; `settingsLocked()` gates them in `emtext.ino`). The only exits
are **BtnA** (turn the portal off) or **PWR** (→Dark). This is the "no nav during setup" +
"show the hotspot password" requirement from §5.

- ⚠️ **Live fields:** the footer's uptime/battery only refresh when the screen redraws on an
  event, so they can read stale — add a periodic redraw while on this screen.
- **Nice-to-haves 🔲:** nicer UI · theme control · choose what appears on Glance · surface ping.

### 4.6 Paused (privacy overlay) ✅
- **On screen:** dim (brightness 60), a large `||` glyph centered, "paused" along the bottom.
  Drawn by `drawPaused()` *before* any state check, so it overrides everything.
- **How you get here:** **BtnB click** from anywhere (including Dark).
- **Effect:** `onPause` sets `g_paused` → `applyMic()` → **mic hard-off** (regardless of mute).
  The privacy stop.
- **Controls:** **BtnB click** → unpause → **Glance** (auto-dims after 8 s). Other callbacks
  still fire, but the overlay keeps drawing until you unpause (privacy wins in `draw()`).
- **Design note:** deliberately unmistakable — distinct brightness, glyph, and word; must never
  be confused with a live read.

### 4.7 Chrome overlays
- **Info bar 🔲:** compact **battery level** + **connectivity indicator**. Today only the
  connection dot exists (green=`Ready`, amber=else; ⚠️ Degraded looks like searching).
- **Clock 🔲:** a tasteful face — **not** larger than the semantic info, **larger** than the
  summary text, tucked to the side and out of the way.
- **Button-press shadows 🔲:** iOS-style shadow rendered near BtnB / PWR to show where the button
  is and give press feedback (like the iOS volume indicator).

---

## 5. Setup-AP portal UX (Stage 4P) ✅

Reconfigure Wi-Fi/host/token from a phone — no cable, no app. Module notes in `firmware/README.md`.

1. **Enter from Settings** (the Wifi SSID row).
2. **AP comes up** — `net` brings up the `emtext-setup` WPA2 hotspot (default pass `emtextsetup`)
   on core 0; Settings switches to the **credentials panel** — `join: <ssid>` / `pass: <apPass>`
   / `at: 192.168.4.1`. Off by default.
3. **Join from the phone** — the captive "sign in" sheet **pops automatically** (the portal
   returns the config form for any URL, tripping the OS reachability probe). Fallback:
   `192.168.4.1`.
4. **Configure** — Wi-Fi network, Wi-Fi password, server host, token. Blank password/token keep
   the current value. **Save & reconnect** writes NVS + `net::reconnect()`.
5. **AP goes off** when you leave setup.

**Changes:**
- ✅ **Nav locked while the portal is up** — `settingsLocked()` freezes scroll + both back
  gestures; only BtnA (turn off) or PWR (→Dark) escape, so state can't drift mid-configure.
- ✅ **Hotspot password shown** on-screen (the credentials panel) so the user can join.
- 🔲 *(nice-to-have)* a **QR code** to join the hotspot / open the page, as a fallback when the
  captive sheet doesn't pop.

⚠️ **TLS is currently insecure** on the active WebSocket backend (documented debt in
`firmware/README.md`) — unrelated to the portal, but must be fixed before real deployment.

---

## 6. Tone → visual mapping ✅

The mark is the **Undertale "DETERMINATION" heart** (`display::drawHeart`, a 16×16 pixel sprite),
rendered as the hero at glance centre. **One shape for every tone — the emotion is the colour**
(Undertale soul colours, pure colour-only by design).

| Tone | Mark | Colour (`color565`) — Undertale soul | History tick |
|---|---|---|---|
| positive | heart | red `(255,40,40)` — Determination | red |
| negative | heart | blue `(60,130,255)` — Integrity | blue |
| sarcastic | heart | purple `(180,70,230)` — Perseverance | purple |
| mixed | heart | yellow `(255,210,40)` — Justice | yellow |
| neutral | heart | dim gray `(120,120,120)` | *none* |

Low-confidence reads render the caption in a fainter gray. Neutral = dim gray heart + no tick
(quiet is the message). This visual mapping is the **only** channel for the emotional signal —
the device has no speaker (audio cues removed). Note: since every tone is the same heart, **colour
is the sole cue** — chosen deliberately, mirroring Undertale (not colour-blind redundant).

---

## 7. Screen transition map (target)

```
        power on
           │  (splash + Quick Setup 🔲)
           ▼
        ┌──────┐  BtnA / lift          ┌────────┐  BtnA          ┌───────────┐
        │ Dark │ ────────────────────► │ Glance │ ─────────────► │ More Info │
        │      │ ◄──────────────────── │        │ ◄───────────── │           │
        └──────┘   8 s glance timeout  └────────┘      BtnA      └───────────┘
           ▲  ▲                            │  ▲                        │
           │  │ BtnA hold = record clip 🔲 │  │ unpause                │
   PWR ────┘  │ (from Dark/Glance)         │  │                        │
           │                               ▼  │                        │
   BtnB hold (from any live screen) ──► ┌──────────┐ ◄── BtnB hold ────┘
           │                            │ Settings │
           │                            └──────────┘
           │                          BtnB = scroll · BtnA = select ✅
           │                          BtnB hold / BtnA hold → Glance · PWR → Dark
           │                               │  wifi row → launch portal
           │                               ▼
           │                     [ emtext-setup AP + captive portal ]
           │                     (device nav LOCKED while up ✅)
           │
   BtnB click (from ANY screen, incl. Dark) ──► ┌────────┐
                                                │ Paused │  (mic hard-off, brightness 60)
                                                └────────┘
                                                   │  BtnB click
                                                   ▼
                                                Glance

   ⚠️ read frame while Dark: updates buffers, does NOT wake the screen (open question).
   ⚠️ read frame while on Glance: refreshes in place and resets the 8 s timer.
```

---

## 8. ⚠️ Gaps & rough edges — the TODO list

Built-UI gaps (each names where it lives):

- [ ] **Read while Dark doesn't wake the screen** (`emtext.ino:onNetFrame` + `display::setGlance`).
      Decide: wake on every read / only non-neutral / cue-only. Most important call here.
- [ ] **Transcript is dead data** (`display.cpp:drawGlance`, `emtext.ino:g_lastTranscript`).
      Surface it in More Info or drop it.
- [ ] **More Info rows overflow** (`display.cpp` History case) — not width-clipped; reuse
      `capWords`/`wrap2`.
- [ ] **Degraded looks like searching** (`display.cpp:drawConnDot`) — give Degraded its own dot.
- [ ] **No "mic live" affordance while Dark** (compliance).
- [ ] **Settings/More Info never auto-dim** (`display.cpp:loop`) — Settings now has explicit back
      (BtnB/BtnA hold) + PWR→Dark, but neither auto-dims on idle. Add an idle timeout.
- [ ] **Static fields on Settings** — footer uptime/battery only refresh on redraw; add a periodic
      redraw while on-screen. (ping isn't surfaced at all — net has no RTT yet.)

New target work (from this plan):

- [x] **Settings page** ✅ built — Status is now a scrollable page (wifi/bright/power) with a
      portal-lock credentials panel. Pending hardware verify.
- [x] **Mute removed** — with audio output cut there is nothing to mute; the Settings `cues` row
      and the mute glyph are dropped from the plan. BtnA-hold stays free for clips. The dormant
      `display::setMuted` / mute-glyph code (`display.cpp:307`, never triggered) can be deleted in
      a cleanup pass.
- [ ] **Rename + rework** History → **More Info** (detail/summary). Still a 5-row list in code.
- [ ] **Clips** — BtnA-hold flags the last utterance to the server for later review (§9.7):
      additive `save` frame + on-screen confirm; server-side file storage + review page.
      (BtnA-hold currently logs a stub.)
- [ ] **Info bar** — battery + connectivity, compact.
- [ ] **Clock** — sized/placed per §4.7.
- [ ] **Button-press shadows** for BtnB / PWR.
- [ ] **Quick Setup at boot** + boot-stage progress.
- [x] **Portal:** ✅ lock nav while up · ✅ show hotspot password. QR fallback still 🔲.
- [x] **Emotion mark decided** ✅ — the **Undertale "DETERMINATION" heart** (`drawHeart`), colored
      by Undertale soul colours (pure colour-only). Replaced the old curve mark (`drawMark`).

---

## 9. 🔲 Planned UI (not built) — maps to remaining stages

### 9.1 Boot stages + Quick Setup (Stage 8 + this plan)
Replace the fixed splash with network→server→ready progress (+ per-stage failure), and a path
into the setup portal from boot.

### 9.2 Audio cues — ❌ REMOVED
The device has **no speaker output**. The emotional signal is conveyed **visually only** (the
glance tone edge bar, §6). There are no tone cues, no alert/boot/low-battery tones, no audible
idle warning, and no mute control. *(Superseded: ≤150 ms cues for negative + words–voice
mismatch, with a 75% battery volume cap and a Settings mute toggle.)*

### 9.7 Clips — server-stored, reviewed later 🔲
BtnA-hold (from Dark/Glance) **saves the current moment to the server** so it can be reviewed
later on a page/dashboard — not kept on the device (it has no storage beyond NVS config).

- **Flow:** BtnA-hold → device sends a small control frame to the server → brief "clip saved"
  confirmation on screen.
- **Recommended payload (open q §10.1):** flag the **last utterance/read `id`**. The server
  already has that utterance's audio, transcript, tone and read, so the device sends only the id
  — no re-streaming audio. Device already tracks the last read id + `g_lastTranscript`.
- **Wire protocol:** an **additive** device→server JSON frame, e.g. `{"type":"save","id":<n>}`.
  Additive-only is required by the protocol contract, and the server already ignores unknown
  types, so nothing else breaks. Server replies (e.g. `{"type":"saved","id":<n>}`) so the device
  can confirm on-screen.
- **Server side (new work, outside firmware):** retain the flagged utterance's
  {audio, transcript, tone, read, timestamp} and expose it for later review. ⚠️ The brief says
  **no database** and **no default retention of third-party audio** — clips are **file-based**
  (a clips folder / JSON index, not a DB) and **user-initiated** (explicit, visible retention,
  not default), which is the intended way to stay within both rules. This is a deliberate scope
  addition to confirm.
- **Compliance note:** because a clip is an explicit user action that retains audio, the "clip
  saved" confirmation doubles as the visible-retention signal.

### 9.3 Two-step power-off (Stage 8)
Replace the `onPowerOff` stub with a deliberate two-step + a **clean WebSocket close** before
sleep. (PWR-hold currently intended → Dark as an interim.)

### 9.4 Idle / motion auto-off (Stage 8)
Auto power-off after no-speech, **extended by motion** (IMU); on-screen grace cancellable by any
button (no audible warning — audio output removed); deep sleep + wake-on-button.

### 9.5 Low battery (Stage 8)
Signal low battery **once**, with a distinct on-screen indication (no tone — audio output removed).

### 9.6 Degraded / outage visibility (Stage 9)
Interruption visible on screen throughout a drop + recovery, not just a silent amber dot.

---

## 10. Decisions & open questions

**Decided:**
- ✅ **On-device audio output removed** — the pendant has no speaker: no tone cues, no
   alert/boot/low-battery tones, no audible idle warning, and no mute. The emotional signal is
   **visual only** (the glance tone edge bar, §6). The `cues` module (Stage 7) is dropped; the
   Settings mute row and mute glyph go with it. BtnA-hold stays free for clips.
- ✅ **Clips are stored on the server, reviewed later** (not kept on-device). See §9.7 for the
   flow and wire-protocol addition.

**Still open:**
1. **Clip payload:** flag just the last utterance's **id** (server keeps the audio + transcript +
   read it already has) vs. also saving audio. Recommended: **flag by id** — cheapest, no
   re-sending audio, and the server already holds everything (§9.7).
2. **Read-while-Dark:** wake on every read, only non-neutral, or cue-only?
3. ~~**Tone symbol:** keep the mark-as-hero curve, or revise the symbol set?~~ **DECIDED** — the
   Undertale heart, coloured by Undertale soul colours, pure colour-only (§6).
4. **Transcript:** show it in More Info, or drop it?
5. **Settings back/idle:** explicit back (BtnB-hold/BtnA-hold→Glance, PWR→Dark) plus an idle
   timeout?

*Keep this doc in sync when display/controls behavior changes — it's the single place to re-read
the whole device UX.*

# emtext pendant — UI/UX reference

A complete walkthrough of the device's on-screen UI and physical interactions: every screen,
what it looks like, how you get in and out, what every button and gesture does, and every
transition between them. It doubles as a **TODO** — rough edges in the built UI are flagged
⚠️, and UI that isn't built yet is in its own section 🔲.

**Source of truth:** this describes the actual code, not an aspiration. Behavior comes from
`src/display/display.cpp` (screens/rendering), `src/controls/controls.cpp` (button + IMU
events), `emtext.ino` (what each event *does* — the wiring), and `src/portal/portal.cpp`
(setup portal). If the doc and the code ever disagree, the code wins — fix the doc.

**Legend:** ✅ built & on-device · 🔲 planned, not built · ⚠️ gap / rough edge (a TODO).

---

## 1. UX principles (why it looks the way it does)

- **The emotion mark is the hero.** The glance leads with a large tone-colored *curve*, not
  text. The words are a small supporting caption. The device's job is to convey *how something
  was said* at a glance; the transcript/read is backup, not the headline.
- **Desaturated palette = observation, not alarm.** Colors are muted on purpose (a green of
  `(90,170,90)`, a red of `(200,80,70)`). Saturated `TFT_RED`/`TFT_GREEN` read as an alert;
  muted tones read as a calm note.
- **Redundant encoding — never color alone.** Tone is carried by *both* the curve shape
  (smile/frown/wave/flat) *and* color, so it survives color-blindness and a dim screen.
- **Neutral = absence of signal.** Neutral draws a flat dim line and *no* history tick. When
  there's nothing worth flagging, the UI stays quiet.
- **No scrolling text, no continuous animation.** Everything is a static frame; the screen only
  changes when something actually happens. This keeps it glanceable and saves power.
- **Privacy is unmistakable.** Pause is a distinct dim screen with a `||` glyph and the word
  "paused" — it can't be confused with any read screen, and it hard-stops the mic.

---

## 2. Screen map

Four screens (`display::State`) plus a **Paused** overlay that is a separate flag drawn *ahead
of* everything, and three small indicator overlays that ride on top of the live screens.

| Screen | Brightness | Purpose | Enter from | Leave to |
|---|---|---|---|---|
| **Boot splash** | (default) | "emtext" logo, 600 ms | power on | Dark (auto) |
| **Dark** ✅ | 0 (off) | resting/idle; screen truly black | boot, 8 s glance timeout | Glance (BtnA / lift) |
| **Glance** ✅ | 130 | the live read: emotion mark + caption | Dark→BtnA/lift, unpause, "else"→BtnA | History (BtnA), Dark (8 s), Status (BtnB-hold), Paused (BtnB) |
| **History** ✅ | 130 | last 5 reads, most-recent-first | Glance→BtnA | Glance (BtnA), Status (BtnB-hold), Paused (BtnB) |
| **Status** ✅ | 130 | wifi/batt/uptime + setup-AP toggle | BtnB-hold (any screen) | AP toggle (BtnA, stays), Paused (BtnB) |
| **Paused** ✅ (overlay) | 60 | privacy screen; mic hard-off | BtnB click (any screen) | Glance (BtnB click again) |

**Indicator overlays** (drawn only when *not* Dark and *not* Paused):

| Overlay | Where | Meaning |
|---|---|---|
| Connection **dot** ✅ | top-right | green = net `Ready`; amber = anything else |
| **Mute glyph** ✅ (circle + slash) | bottom-left | mic muted (BtnA-hold) |
| **Processing "…"** ✅ | top-left (Glance only) | server sent `status: thinking`; a read is pending |

---

## 3. Control reference

`M5.update()` runs first in `loop()`, then `controls::loop()` maps raw buttons/IMU to semantic
callbacks; `emtext.ino` decides what each one does.

| Input | Event | Action (context-dependent) | Wired in |
|---|---|---|---|
| **BtnA** click | `onWake` | Dark→Glance · Glance→History · Status→toggle AP · else→Glance | `emtext.ino:onWake` |
| **BtnA** hold | `onMute` | toggle mute (mic off if muted-or-paused; mute glyph) | `emtext.ino:onMute` |
| **BtnB** click | `onPause` | toggle privacy pause (mic hard-off; Paused overlay) | `emtext.ino:onPause` |
| **BtnB** hold | `onStatus` | go to Status screen | `emtext.ino:onStatus` |
| **BtnPWR** hold | `onPowerOff` | ⚠️ **stub** — logs only, no shutdown, no UI | `emtext.ino:onPowerOff` |
| **IMU** tilt | `onOrient(rot)` | auto-rotate the screen (portrait/landscape) | `emtext.ino:onOrient` |
| **IMU** lift | `onLift` | if Dark → Glance (lift-to-wake) | `emtext.ino:onLift` |

**Timings & thresholds:**
- Glance auto-dims to Dark after **8 s** (`GLANCE_MS`), reset on each new read.
- Click vs hold is M5Unified's own `wasClicked()` / `wasHold()` (hold ≈ 500 ms default).
- IMU sampled ~**20 Hz** (every 50 ms). Orientation fires only when the quadrant changes.
- Lift-to-wake: acceleration magnitude > **1.35 g**, then a **1.5 s** cooldown so it fires once.
  Both are coarse and meant to be tuned on hardware (`LIFT_G`, `LIFT_COOLDOWN_MS`).

---

## 4. Per-screen walkthrough

### 4.1 Boot splash ✅
- **On screen:** "emtext" centered, text size 2, green on a real M5StickS3 / red on any other
  board (a quick board-detect sanity check), on black. Shown for 600 ms.
- **How you get here:** power on / reset.
- **Controls:** none (it's a fixed delay in `setup()` before the display module takes over).
- **Transitions out:** after 600 ms, `display::begin()` sets **Dark** (brightness 0). Fake seed
  data is loaded (`"hey, nice work"`, positive) so the first wake shows something.
- ⚠️ **Gap:** the splash is a dumb delay — it shows nothing about conn(wifi/server) progress.
  See 🔲 Boot stages (6.1). Also the board-color check is dev scaffolding, not real UX.

### 4.2 Dark ✅
- **On screen:** nothing — brightness 0, black. The device's normal resting state.
- **How you get here:** after boot; after the 8 s glance timeout.
- **Controls from here:**
  - **BtnA click** → Glance.
  - **Lift gesture** → Glance.
  - **BtnB click** → Paused (privacy) — works even from Dark.
  - **BtnB hold** → Status.
  - **BtnA hold** → toggles mute (no visible effect while Dark; glyph appears next time a live
    screen draws). ⚠️ muting while Dark gives no feedback.
- **Transitions out:** Glance (BtnA/lift), Status (BtnB-hold), Paused (BtnB click).
- ⚠️ **Key gap:** a `read` arriving while Dark **updates history and the glance buffer but does
  not wake the screen**. During a live conversation with the screen dark, reads accumulate
  silently. Decide the intended behavior (see 6-Gaps and 8-Transition map).
- ⚠️ **Compliance gap:** while Dark the mic may be *live* but there is no external indication of
  capture state (the brief wants capture-state externally visible).

### 4.3 Glance ✅ — the hero screen
- **On screen (top to bottom):**
  - Connection **dot**, top-right (green/amber).
  - **Emotion mark** at ~40% height: a tone-colored curve drawn as overlapping dots across ~55%
    of the width — **smile** (positive, green), **frown** (negative, red), **wave**
    (sarcastic/mixed, amber), **flat dim line** (neutral).
  - **Read caption** near the bottom: the read text, capped to **≤6 words** and wrapped to **≤2
    lines**, in dim gray — or *fainter* gray when the read is low-confidence. This is deliberately
    small: the mark leads, the words support.
  - **"…"** top-left if a read is still pending (`status: thinking`).
  - **Mute glyph** bottom-left if muted.
- **How you get here:** Dark→BtnA/lift; unpause; the "else" branch of `onWake`; a new read while
  already on Glance refreshes it in place.
- **Controls from here:**
  - **BtnA click** → History.
  - **BtnB hold** → Status.
  - **BtnB click** → Paused.
  - **BtnA hold** → mute toggle.
- **Transitions out:** History (BtnA), Status (BtnB-hold), Paused (BtnB), **Dark after 8 s** of
  no new read.
- ⚠️ **Gap — transcript is dead data:** `emtext.ino` captures the `utterance` transcript into
  `g_lastTranscript` and passes it to `display::setGlance(...)`, but the current `drawGlance`
  renders only the mark + read caption — the transcript is never shown. Either surface it
  (e.g. a press-to-reveal, or on History) or drop the argument.
- ⚠️ **Open decision — emotion-mark vs. revert:** the mark-as-hero design is the current build.
  Whether to keep it or revert to a text-led glance is still open (see 6-Gaps).

### 4.4 History ✅
- **On screen:** "history" title top-left; then up to **5** most-recent reads, newest first.
  Each row = a small **tone tick** (colored rectangle; *none* for neutral) + an ordinal label
  (`now`, `1 ago`, `2 ago`, …) + the read text, dim gray. If empty: "(nothing yet)".
- **How you get here:** Glance → BtnA click.
- **Controls from here:**
  - **BtnA click** → Glance.
  - **BtnB hold** → Status.
  - **BtnB click** → Paused.
  - **BtnA hold** → mute toggle.
- **Transitions out:** Glance (BtnA), Status (BtnB-hold), Paused (BtnB). ⚠️ **History does not
  auto-dim** — only Glance has the 8 s timeout, so History stays lit until you act.
- ⚠️ **Gap — rows not width-clipped:** row text is drawn raw at x=10; long reads run off the
  right edge. Reuse `capWords`/`wrap2` (already in `display.cpp`) to clip.
- ⚠️ **Gap — "now" is redundant** with the Glance you just left; consider relabeling, or showing
  relative time instead of ordinals.

### 4.5 Status ✅
- **On screen:** "status" title (white); then `wifi: ready|searching`, `batt: N%`,
  `up: <seconds>s`; a setup-AP block — **ON**: `setup AP: ON <ssid>` + `join <ip>` in amber;
  **off**: `setup AP: off` in dim gray; and the hint `[A] toggle AP` in faint gray.
- **How you get here:** **BtnB hold** from any live screen.
- **Controls from here:**
  - **BtnA click** → **toggles the setup AP** (this is the *only* screen where BtnA does this).
    Turning it on brings up the `emtext-setup` hotspot and starts the captive portal (see §5).
  - **BtnB click** → Paused.
  - **BtnB hold** → (re)enters Status.
  - **BtnA hold** → mute toggle.
- **Transitions out:** stays on Status across an AP toggle (the block updates in place); Paused
  (BtnB click). ⚠️ **No direct "back":** there's no explicit exit to Glance/Dark other than the
  8 s-less screens — you leave via Pause, or wait (Status also does **not** auto-dim). Consider a
  back affordance or a timeout.
- ⚠️ **Gap — uptime/battery are static:** the screen only redraws on events (AP toggle, net
  change), so `up:`/`batt:` are stale until something forces a redraw.

### 4.6 Paused (privacy overlay) ✅
- **On screen:** dim (brightness 60), a large `||` glyph centered, "paused" along the bottom.
  Drawn by `drawPaused()` *before* any state check, so it overrides Glance/History/Status.
- **How you get here:** **BtnB click** from anywhere (including Dark).
- **Effect:** `onPause` sets `g_paused` and calls `applyMic()` → the **mic is hard-off** while
  paused (regardless of mute). This is the privacy stop.
- **Controls from here:**
  - **BtnB click** → unpause → **Glance** (which then auto-dims after 8 s).
  - Other controls still fire their callbacks, but the Paused overlay keeps drawing until you
    unpause (privacy takes precedence in `draw()`).
- **Transitions out:** Glance (BtnB click).
- **Design note:** deliberately unmistakable — distinct brightness, distinct glyph, a word. The
  one screen that must never be confused with a live read.

### 4.7 Indicator overlays ✅
- **Connection dot** (top-right): green when `net::state() == Ready`, amber otherwise. Driven
  from `loop()` in `emtext.ino` whenever net state changes → `display::setConnection`.
  ⚠️ **Degraded looks like searching** — both render amber; the `Degraded` state (socket alive,
  no server traffic) is not visually distinct.
- **Mute glyph** (bottom-left): amber circle with a slash, shown when muted. Only visible on a
  live screen.
- **Processing "…"** (top-left, Glance only): amber, shown between `status: thinking` and the
  read landing. Cleared when the read arrives.

---

## 5. Setup-AP portal UX (Stage 4P) ✅

Reconfigure Wi-Fi/host/token from a phone, with no cable and no app. Full module notes are in
`firmware/README.md`; the UX flow:

1. **Enter Status** — BtnB hold. It shows `setup AP: off` + `[A] toggle AP`.
2. **Toggle the AP on** — BtnA. `net` brings up the `emtext-setup` WPA2 hotspot (default pass
   `emtextsetup`) on core 0; the Status block flips to `setup AP: ON emtext-setup` / `join
   192.168.4.1`. The AP is **off by default** and only exists while toggled on.
3. **Join from the phone** — connect to `emtext-setup`. The **captive "sign in" sheet pops
   automatically** (the portal returns the config form for *any* URL, so the OS reachability
   probe triggers it). Manual fallback: browse to `192.168.4.1`.
4. **Configure** — the page has Wi-Fi network, Wi-Fi password, server host, access token.
   Blank password/token fields keep the current value. **Save & reconnect** writes NVS and
   triggers `net::reconnect()`.
5. **Toggle the AP off** — BtnA again on Status.

⚠️ **AP-up latency:** if STA is *failing* when you toggle, the AP can take up to ~10 s to appear
(the net task is busy in a connect attempt); instant when STA is already connected. ⚠️ **TLS is
currently insecure** on the active WebSocket backend (documented debt in `firmware/README.md`) —
unrelated to the portal, but relevant before any real deployment.

---

## 6. Tone → visual mapping ✅

| Tone | Mark shape | Color (`color565`) | History tick | Cue (🔲 planned) |
|---|---|---|---|---|
| positive | smile (upward arc) | green `(90,170,90)` | green | silent |
| negative | frown (downward arc) | red `(200,80,70)` | red | short tone |
| sarcastic | wave | amber `(220,160,40)` | amber | mismatch tone |
| mixed | wave | amber `(220,160,40)` | amber | mismatch tone |
| neutral | flat dim line | dim `(150,150,150)` | *none* | silent |

- **Low-confidence** reads: the caption is drawn in the fainter gray `(90,90,90)` instead of the
  normal dim gray — the mark still shows, the words recede.
- **Neutral = absence:** no tick in history, flat line on the glance. Quiet is the message.

---

## 7. Screen transition map ✅

```
        power on
           │  (600 ms splash)
           ▼
        ┌──────┐  BtnA / lift          ┌────────┐  BtnA          ┌─────────┐
        │ Dark │ ────────────────────► │ Glance │ ─────────────► │ History │
        │      │ ◄──────────────────── │        │ ◄───────────── │         │
        └──────┘   8 s glance timeout  └────────┘      BtnA      └─────────┘
           ▲                               │  ▲                       │
           │       (History/Status do NOT  │  │ unpause               │
           │        auto-dim)              │  │                       │
           │                               ▼  │                       │
   BtnB hold (from any live screen) ──► ┌────────┐                    │
           │                            │ Status │ ◄──── BtnB hold ───┘
           │                            └────────┘
           │                               │  BtnA = toggle setup AP (stays on Status)
           │                               ▼
           │                          [ emtext-setup AP + captive portal ]
           │
   BtnB click (from ANY screen, incl. Dark) ──► ┌────────┐
                                                │ Paused │  (mic hard-off, brightness 60)
                                                └────────┘
                                                   │  BtnB click
                                                   ▼
                                                Glance

   ⚠️ read frame while Dark: updates history + glance buffer, but does NOT wake the screen.
   ⚠️ read frame while on Glance: refreshes in place and resets the 8 s timer.
```

---

## 8. ⚠️ Gaps & rough edges (built UI) — the TODO list

Each item names where it lives so it's actionable.

- [ ] **Read while Dark doesn't wake the screen** (`emtext.ino:onNetFrame` + `display::setGlance`).
      Reads pile up invisibly during a live conversation. Decide: wake on every read / only on
      non-neutral / never (cue-only). This is the most important UX decision here.
- [ ] **Transcript is dead data** (`display.cpp:drawGlance`, `emtext.ino:g_lastTranscript`).
      Captured and passed but never rendered. Surface it (press-to-reveal / History detail) or
      drop the argument.
- [ ] **History rows overflow** (`display.cpp` History case). Not width-clipped; reuse
      `capWords`/`wrap2`.
- [ ] **Degraded looks like searching** (`display.cpp:drawConnDot`, `emtext.ino` loop). Give net
      `Degraded` its own dot color/blink so an outage-in-progress is distinguishable.
- [ ] **No "mic live" affordance while Dark** (compliance). Capture state should be externally
      visible when the mic is on; today Dark shows nothing.
- [ ] **First-run guidance** (`display.cpp` Status/Glance). When never configured, hint the user
      toward BtnB-hold → toggle AP instead of sitting on "searching" forever.
- [ ] **Status/History never auto-dim** (`display.cpp:loop`). Only Glance times out; the others
      stay lit. Consider a shared idle timeout or a back affordance.
- [ ] **Static fields on Status** (`display.cpp` Status case). `batt:`/`up:` only refresh on an
      event; add a periodic redraw while on Status.
- [ ] **Muting while Dark is silent** (`emtext.ino:onMute`). No feedback until a live screen draws.
- [ ] **History "now" label** duplicates the Glance read; relabel or use relative timestamps.
- [ ] **Emotion-mark keep-vs-revert** — OPEN DECISION. The mark-as-hero glance (`drawMark` +
      caption) is the current build; a text-led alternative was the prior design. Pick one.

---

## 9. 🔲 Planned UI (not built yet)

Designed here so the UX is decided before it's coded. These map to the remaining firmware stages.

### 9.1 Boot stages (Stage 8)
Replace the dumb 600 ms splash with visible progress: **network → server → ready**, each
distinguishable on screen (and later by tone), including per-stage *failure* (e.g. "wifi failed",
"server unreachable"). The connection dot already encodes ready/not-ready once past boot; boot
should show the same progression before the first Glance.

### 9.2 Audio cues (Stage 7, `cues` module)
Short speaker tones **≤150 ms**: a distinct cue for **negative** tone and for **words–voice
mismatch** (sarcastic/mixed); **silent for neutral/positive**. Mute is the existing BtnA-hold
(single action, already toggles the glyph). Cap volume to **75%** while on battery. Must not
disturb mic capture (shared-I2S check). Cue column is stubbed in the tone table (§6).

### 9.3 Two-step power-off (Stage 8)
Replace the `onPowerOff` stub (BtnPWR hold) with a deliberate two-step: an on-screen confirm,
then a **clean WebSocket close** before sleep, so the server sees a graceful disconnect.

### 9.4 Idle / motion auto-off (Stage 8)
Auto power-off after a no-speech period, **extended by motion** (IMU): stationary+silent may
power off; in-motion+silent stays on. Before it sleeps, an **audible warning + on-screen grace
period** cancellable by any button. Deep sleep with **wake on button**.

### 9.5 Low battery (Stage 8)
Signal low battery **once** (not a nagging repeat) — a distinct screen and/or tone.

### 9.6 Degraded / outage visibility (Stage 9)
The connection interruption must be **visible on screen throughout** a drop and recovery (not
just a silent amber dot). Pairs with the Degraded-dot gap in §8.

---

## 10. Open questions (decide these next)

1. **Read-while-Dark:** wake on every read, only non-neutral, or cue-only? (drives §8 item 1)
2. **Emotion mark:** keep the mark-as-hero glance, or revert to text-led?
3. **Transcript:** show it somewhere, or drop it?
4. **Screen back/idle:** should History/Status auto-dim or get an explicit back?

*Keep this doc in sync when the display/controls behavior changes — it's meant to be the single
place to re-read the whole device UX.*

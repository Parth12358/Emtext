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
| **Boot** | `display::setBoot` | ✅ boot stages · 🔲 quick setup | logo + network→server→ready progress (shapes) while connecting |
| **Dark** | `State::Dark` | ✅ | resting/idle; screen black; default state |
| **Glance** | `State::Glance` | ✅ | the live read: tone symbol + LLM context + chrome |
| **More Info** | `State::History` | ✅ (as history) | longer LLM context / summary of recent reads |
| **Settings** | `State::Status` | ✅ scrollable page | rows: wifi/bright/power + portal-lock panel |
| **Paused** (overlay) | `g_paused` flag | ✅ | privacy screen; mic hard-off |
| **Sleep grace** (overlay) | `graceOn` | ✅ | pre-sleep countdown (crescent moon + number); any button/motion cancels (§9.4) |
| **Low battery** (overlay) | `lowBattUntil` | ✅ | transient one-time warning (§9.5) |

**Chrome overlays** (target — ride on top of the live screens):

| Overlay | Now | Target |
|---|---|---|
| **Top bar** (connectivity + ping · battery) | ✅ `drawTopBar` — connectivity by **shape** (`drawConnGlyph`: filled disc = ready, hollow ring = searching, ring+dot = degraded — colour-blind safe, colour is only a second cue), **live ping** (median RTT in ms, off the 15 s keepalive pong — no extra traffic), and **battery as a number** (`NN%`, right side). Top strip in portrait (battery as `NN%` text); left strip in landscape where battery is a small **icon** (`drawBattIcon`, proportional fill — the stacked digits were illegible) with ping stacked below and the charging **lightning bolt** (`drawBolt`, shape not colour). | — |
| **Press indicators** (A/B/PWR) | ✅ `drawIndicators` — three edge segments, rest a **very dark blue**, light on press (A red / B white / PWR green), on the physical-button edge, orientation-aware. Bar thickness = `STRIP` (12 px), shared with the top status bar so they match. | — |
| **Clock** | 🔲 | small tasteful face, on the side/out of the way; larger than the summary text, **smaller** than the semantic read |
| **Processing "…"** | ✅ | shown while a read is pending (`status: thinking`) |

Both the top bar and the press indicators render on **every lit screen except Dark** (Paused stays clean). Screen content (titles, read, footer) was nudged inward to leave room for them.

**Dev activity glyphs** (on the top bar, distinct shapes — colour-blind safe): **dot** = listening (`audio::voiced()`), **▲** = sending audio, **▼** = receiving a frame, **■** = clip fired. Sending/receiving flash from `net::lastTxMs()`/`lastRxMs()` (a ~250 ms window); clip from a ~500 ms window. A centred cluster in portrait; a 2×2 grid at the top of the strip in landscape. Debug aid, not end-user UI.

---

## 3. Control reference

`M5.update()` runs first in `loop()`, then `controls::loop()` maps raw buttons/IMU to semantic
callbacks; `emtext.ino` decides what each does — **context-dependent on the current screen**.

| Input | Action (by screen) | Wired |
|---|---|---|
| **BtnA** click | Dark→Glance · Glance→More Info · More Info→Glance · **Settings→select row** | `onWake` ✅ |
| **BtnA** hold | Settings→back to Glance · elsewhere→**hold-to-record a clip** (§9.7; server-side `save`/`saved` built) | `onHoldA` ✅ |
| **BtnB** click | **Settings→scroll to next row** · elsewhere→privacy pause | `onPause` ✅ |
| **BtnB** hold | live screen→open Settings · **Settings→back to Glance** | `onStatus` ✅ |
| **BtnPWR** press | **hardware off** — the M5PM1 PMIC power key; firmware can't intercept it (no firmware power-off gesture) | — (hardware) |
| **IMU** tilt | auto-rotate | `onOrient` ✅ |
| **IMU** lift | Dark → Glance (lift-to-wake) | `onLift` ✅ |

While the **setup portal is up**, Settings nav is **locked**: BtnB scroll and both back gestures
are dead; only BtnA (turn portal off) gets you out.

During the **pre-sleep grace countdown**, any button or motion cancels. There is no power-off confirm
screen — deliberate shutdown is the automatic idle path; PWR is the physical off switch.

**Timings & thresholds (Now):** Glance auto-dims to Dark after **8 s** (`GLANCE_MS`, reset on
each read); History/Settings after **20 s** (`IDLE_MS`). Separately, **power auto-sleep** (Stage 8)
fires after `idleTimeoutS` (**300 s**) of silence + stationary, with a `graceS` (**10 s**) countdown,
then **low-power standby** (Wi-Fi kept, button-only wake) up to `standbyMaxS`, then full power off.
**Dev mode** (`devMode`, **default OFF**) disables all auto-dim (screen stays awake), lights
the glance from boot, **and disables auto-sleep**; enable it for bench work with `set dev 1` + `save`
on the serial console.
Click vs hold = M5Unified `wasClicked()`/`wasHold()` (~500 ms). IMU ~**20 Hz**;
orientation is **debounced** (`ORIENT_STABLE_MS` = **500 ms**) — a new quadrant must hold steady
that long before it flips, so it can't flicker near the 45° boundary. Lift = accel > **1.35 g** then a **1.5 s** cooldown
(`LIFT_G`, `LIFT_COOLDOWN_MS`). Motion for the auto-sleep gate = accel off **1 g** rest by > **0.08 g**
(`MOTION_G`, `controls::lastMotionMs()`). All three are coarse — **tune on hardware**.

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
  - **BtnA hold** → ✅ **record a clip** — wakes to Glance with the recording overlay; release
    saves what was heard during the hold to the server (§9.7).
- **Transitions out:** Glance (BtnA/lift), Settings (BtnB-hold), Paused (BtnB click),
  clip recording (BtnA hold → Glance + overlay).
- ⚠️ **Read while Dark doesn't wake the screen** — reads accumulate silently (open question §10).
- ⚠️ **No "mic live" affordance while Dark** — capture state should be externally visible
  (compliance).

### 4.3 Glance — the hero screen
**On-screen info (target):**
- **Emotional tone** as a symbol (the hero). ✅ built as the Undertale heart (`drawHeart`),
  colored by tone with Undertale soul colours (§6).
- **Context from the LLM** (the read). ✅ built as a small caption.
- **Connectivity** to server + wifi. ✅ top bar — connectivity glyph (shape) + live ping (§2).
- **Battery level.** ✅ in the top bar (`NN%` in portrait, icon + charging bolt in landscape).
- **Clock / time.** 🔲
- **iOS-style button shadows** to hint BtnB/PWR. 🔲

**Nice-to-haves 🔲:** animations · wifi range/ping detail · battery level with uptime-remaining.

- **How you get here:** Dark→BtnA/lift; unpause; a new read while already on Glance refreshes it
  in place.
- **Controls from here:**
  - **BtnA click** → **More Info** (renamed from History).
  - **BtnB hold** → Settings.
  - **BtnB click** → Paused.
  - **BtnA hold** → ✅ record a clip (§9.7); the glance stays up under the recording overlay.
- **Transitions out:** More Info (BtnA), Settings (BtnB-hold), Paused (BtnB), **Dark after 8 s**
  of no new read.
- **Transcript is not on the glance by design** — the heart + read are the whole screen. It is
  carried through `setGlance` into the history ring and rendered in More Info (§4.4).

### 4.4 More Info — `State::History` ✅ (being repurposed)
- **Now ✅:** "more info" title + the **3** most recent reads (of the 5-deep ring), each a tone
  tick + the read, with the **transcript as a dimmer second line**; no ordinal label;
  most-recent-first; "(nothing yet)" when empty. Every line is width-clipped via `wrapN`.
- **Target 🔲:** less of a 5-row list, more of a **detail page** — longer LLM context and a
  summary of the last few prompts/reads. The place you go when the glance caption isn't enough.
- **Controls from here:**
  - **BtnA click** → Glance.
  - **BtnB hold** → Settings.
  - **BtnB click** → Paused.
  - **BtnA hold** → ✅ starts recording a clip and switches to Glance (recording overlay).
- **Auto-dims** to Dark after `IDLE_MS` (20 s) with no interaction (Glance stays at 8 s).

### 4.5 Settings — `State::Status` ✅ (scrollable settings page — built, pending hardware verify)
A scrollable page (`display.cpp:drawSettings()`), 3 rows with a `>` cursor, a `[B]next [A]sel`
hint top-right, and a faint footer aside (`ready | up Ns | NN%`). **Navigation:**
- **BtnB click** → scroll cursor to next row (wraps).
- **BtnA click** → select/activate the highlighted row.
- **BtnB hold** or **BtnA hold** → back to Glance · **PWR** → hardware off.

**Rows ✅:**

| Row (code `Setting`) | Value shown | BtnA select does |
|---|---|---|
| **wifi** (`Wifi`) | joined SSID (trunc.) | launch/stop the setup **portal** |
| **bright** (`Brightness`) | `n/4` | cycle 4 brightness presets (`40/90/150/220`), applied live to all lit screens |
| **power** (`Power`) | battery `%` | informational (battery %); no firmware power-off — PWR is a hardware off |

*(The **cues**/mute row is removed — the device has no audio output, so there is nothing to mute.)*

Dropped from the page per the plan: **URL/host** and **token** (secret) aren't shown. **Ping**
is not a Settings row — `net::pingMs()` (median keepalive RTT) is shown live in the top bar on
every lit screen instead. Uptime/battery live in the footer aside.

**Portal lock ✅:** while the setup AP is up, the page replaces the rows with a credentials
panel — **`join: <ssid>` · `pass: <apPass>` · `at: 192.168.4.1`** — and **freezes navigation**
(scroll + both back gestures dead; `settingsLocked()` gates them in `emtext.ino`). The only exits
are **BtnA** (turn the portal off) or **PWR** (→Dark). This is the "no nav during setup" +
"show the hotspot password" requirement from §5.

- ⚠️ **Live fields:** the footer's uptime/battery only refresh when the screen redraws on an
  event, so they can read stale — add a periodic redraw while on this screen. (The top bar
  already refreshes every ~5 s.)
- **Nice-to-haves 🔲:** nicer UI · theme control · choose what appears on Glance.

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
- **Info bar ✅:** built as the **top bar** (`drawTopBar`, §2) — connectivity **glyph** by shape
  (`drawConnGlyph`: filled = ready, ring = searching, ring+dot = degraded), **live ping** in ms,
  and **battery** (`NN%` in portrait; `drawBattIcon` + charging bolt in landscape). On every lit
  screen except Dark.
- **Clock 🔲:** a tasteful face — **not** larger than the semantic info, **larger** than the
  summary text, tucked to the side and out of the way.
- **Button-press shadows 🔲:** iOS-style shadow rendered near BtnB / PWR to show where the button
  is and give press feedback (like the iOS volume indicator).

---

## 5. Setup-AP portal UX (Stage 4P) ✅

Reconfigure Wi-Fi/host/token from a phone — no cable, no app. Module notes in `docs/guides/firmware.md`.

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
`docs/guides/firmware.md`) — unrelated to the portal, but must be fixed before real deployment.

---

## 6. Tone → visual mapping ✅

The mark is the **Undertale "DETERMINATION" heart** (`display::drawHeart`, a 16×16 pixel sprite),
rendered as the hero at glance centre. **One shape for every tone — the emotion is the colour**
(Undertale soul colours, pure colour-only by design).

Since fw 0.2.0 the colour key is the server's **`emotion.label`** — the seven labels of the
voice model, as adjusted by the interpreter — and the six chromatic souls map onto the six
non-neutral emotions. The five older tone names still resolve (below), so a pre-2026-09-22
server that sends only `tone` keeps its colours.

| Emotion | Mark | Colour (`color565`) — Undertale soul | History tick |
|---|---|---|---|
| angry | heart | red `(255,0,40)` — Determination | red |
| surprised | heart | orange `(255,140,0)` — Bravery | orange |
| happy | heart | yellow `(255,216,0)` — Justice | yellow |
| disgusted | heart | green `(0,192,0)` — Kindness | green |
| fearful | heart | cyan `(66,255,255)` — Patience | cyan |
| sad | heart | blue `(0,120,255)` — Integrity | blue |
| neutral | heart | dim gray `(120,120,120)` | *none* |

| Tone (older servers) | Colour |
|---|---|
| positive | red — Determination |
| negative | blue — Integrity |
| sarcastic | purple — Perseverance |
| mixed | yellow — Justice |
| neutral | dim gray, no tick |

Low-confidence reads render the caption in a fainter gray — driven, since fw 0.1.2, by the
server's `read.confidence.level == "low"` (`server/confidence.py`; absent on older servers, in
which case the caption draws at full weight). Neutral = dim gray heart + no tick
(quiet is the message). This visual mapping is the **only** channel for the emotional signal —
the device has no speaker (audio cues removed). Note: since every tone is the same heart, **colour
is the sole cue** — chosen deliberately, mirroring Undertale (not colour-blind redundant).

---

## 7. Screen transition map (target)

```
        power on / wake (power button)
           │  (boot stages ✅ network→server→ready · Quick Setup 🔲)
           ▼
        ┌──────┐  BtnA / lift          ┌────────┐  BtnA          ┌───────────┐
        │ Dark │ ────────────────────► │ Glance │ ─────────────► │ More Info │
        │      │ ◄──────────────────── │        │ ◄───────────── │           │
        └──────┘   8 s glance timeout  └────────┘      BtnA      └───────────┘
           ▲  ▲                            │  ▲                        │
           │  │ BtnA hold = record clip ✅ │  │ unpause                │
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

   read frame while Dark: WAKES the screen to Glance for ~8 s, then auto-dims (setGlance).
   read frame while on Glance: refreshes in place and resets the 8 s timer.
   (both suppressed while paused / while in History or Settings, which aren't interrupted.)

   PWR = hardware off (M5PM1 power key; firmware can't intercept it, §9.3). Power overlays sit
   above every screen: boot stages (§9.1), pre-sleep grace (§9.4), low-battery (§9.5).
   Auto-sleep (silence+stationary → grace → low-power standby → full off after standbyMaxS) is the
   clean-shutdown path. Standby keeps Wi-Fi connected and wakes on a BUTTON (instant, no reconnect);
   full off wakes on PWR (cold boot). No ESP deep sleep.
```

---

## 8. ⚠️ Gaps & rough edges — the TODO list

Built-UI gaps (each names where it lives):

- [x] **Read while Dark wakes the screen** ✅ (`display::setGlance`) — a server `read` now brings the
      glance up from Dark for ~8 s, then auto-dims (skipped while paused / in History / Settings).
- [x] **Transcript surfaced** ✅ — shown as a dim second line under each read in History (stored in
      the history ring), so the terse 5-word read has its "what was said" context. No longer dead data.
- [x] **History rows width-clipped** ✅ — each line clipped to width with `wrapN`, both orientations.
- [x] **Degraded distinct** ✅ — connectivity is now shape-based (`drawConnGlyph`: filled/ring/ring+dot),
      so Degraded reads differently from Searching — and it's colour-blind safe (the user is colour-blind).
- [ ] **No "mic live" affordance while Dark** (compliance).
- [x] **History/Settings auto-dim** ✅ — idle-dim to Dark after `IDLE_MS` (20s; Glance stays 8s),
      reset on interaction; never during the setup portal or clip recording.
- [x] **Live status** ✅ — the top bar refreshes battery/ping every ~5s (`display::loop`) and ping
      (median RTT) is measured off the keepalive. (Settings' own footer still refreshes on redraw
      only — minor; the top bar carries the live data now.)

New target work (from this plan):

- [x] **Settings page** ✅ built — Status is now a scrollable page (wifi/bright/power) with a
      portal-lock credentials panel. Pending hardware verify.
- [x] **Mute removed** — with audio output cut there is nothing to mute; the Settings `cues` row and
      the mute glyph are dropped. BtnA-hold is now hold-to-record clips. The dormant
      `display::setMuted` / mute-glyph code has been **deleted**.
- [x] **History → "More Info"** ✅ — read + dim transcript per entry (3 entries), a light detail view.
- [x] **Clips** — **hold-to-record, built both sides**: hold BtnA → full-screen recording overlay
      (pause-screen style) + live timer, screen awake; on release → **"saving…"** while it waits a
      short grace for the tail utterance to land, then sends `{"type":"save","from":P+1,"to":<highest
      utterance id>}` (`net::saveClip`) and shows a full-screen **saved/empty** status for a few
      seconds before resuming. Range is tracked from **both** `utterance` and `read` ids (so `user`
      lines + the tail are captured — all spoken text). Server bundles the range into one clip.
      Spec: §9.7 below + `CLAUDE.md` (server side).
- [x] **Info bar** ✅ — done as the top status bar (connectivity glyph + ping + battery %, `drawTopBar`).
- [ ] **Clock** — sized/placed per §4.7.
- [ ] **Button-press shadows** for BtnB / PWR.
- [x] **Boot-stage progress** ✅ — network→server→ready with shape markers (`display::setBoot`, §9.1).
      **Quick Setup at boot** (a path straight into the portal) still 🔲.
- [x] **Portal:** ✅ lock nav while up · ✅ show hotspot password. QR fallback still 🔲.
- [x] **Emotion mark decided** ✅ — the **Undertale "DETERMINATION" heart** (`drawHeart`), colored
      by Undertale soul colours (pure colour-only). Replaced the old curve mark (`drawMark`).

---

## 9. Planned and recently built UI — maps to remaining stages

(9.1 and 9.3–9.5 are ✅ built — Stage 8, validate on hardware; 9.6 is 🔲; 9.2 is removed; 9.7 is ✅ built.)

### 9.1 Boot stages — built ✅ (Stage 8)
The fixed splash is replaced by **network → server → ready** progress driven by `net::state()`
(`display::setBoot(stage, failed)`), on-screen only. Each stage is a **shape** (colour-blind safe):
filled disc = done, hollow ring = in progress, **X** = failed (e.g. auth-rejected), faint ring =
pending. `setup()` blocks on this (≤ 20 s) until `ready`, then hands over to the normal UI.
Quick Setup (a boot path straight into the portal) is still 🔲.

### 9.2 Audio cues — ❌ REMOVED
The device has **no speaker output**. The emotional signal is conveyed **visually only** (the
colour of the glance heart, §6). There are no tone cues, no alert/boot/low-battery tones, no audible
idle warning, and no mute control. *(Superseded: ≤150 ms cues for negative + words–voice
mismatch, with a 75% battery volume cap and a Settings mute toggle.)*

### 9.3 Power-off — hardware only (Stage 8)
There is **no firmware power-off gesture.** On the M5StickS3 the PWR button is the **M5PM1 PMIC's
power key**: a tap does a **hardware power-off** that firmware cannot intercept (M5Unified only ever
reports a "click", never a hold — `getKeyState()` returns 0/2) or retime (the M5PM1 driver exposes no
key-behaviour register). So PWR is the physical off switch. The requirement's *deliberate two-step
power-off with a clean close* is instead satisfied by the **automatic idle path** (§9.4: grace → deep
sleep, `net::shutdown()` first). Wakes on the power button. *(A firmware off gesture could be moved to
a BtnA/BtnB long-press later if desired; not built — the user chose auto-sleep only.)*

### 9.4 Staged auto-sleep — built ✅ (Stage 8)
A staged cycle, **no ESP deep sleep** (pointless for a button-woken, no-BLE device — it restarts the
sketch anyway and draws more than a full off). **Awake → Idle → grace → low-power standby → full off:**
- **Idle** — after the display auto-dim (screen Dark), the device keeps listening (gated streaming) and
  stays connected. This is the existing behaviour.
- **Grace** — after `idleTimeoutS` of **silence + stationary** (speech and IMU motion both reset the
  timer; motion keeps a worn device awake), a **grace countdown** (`graceS`) shows a crescent-moon
  overlay + a big number; **any button or motion cancels**.
- **Low-power standby** — on grace expiry (`power::onStandby(true)`): **mic off, device ping off,
  screen off, Wi-Fi kept connected** (deeper modem sleep). The socket stays alive on the **server's own
  keepalives** (the device stops pinging), so it recovers **instantly on a BUTTON press** — no
  reconnect. **Motion is ignored in standby** (button-only wake). The waking press is consumed (just
  wakes, doesn't also advance the UI).
- **Full power off** — after `standbyMaxS` in standby (kept < the server's ~15-min no-audio close, so
  the link is still live), or on low battery: `power::onSleep` → `net::shutdown()` (clean WS close) →
  `M5.Power.powerOff()`. Recover via **PWR** (cold boot). Disabled in dev mode.

### 9.5 Low battery — built ✅ (Stage 8)
Below `lowBattPct` (and not charging) a **one-time** transient warning shows a near-empty battery
outline (a shape) + "!" + "low battery" (no tone — audio output removed). Hysteresis + charge state
reset it so it doesn't repeat.

### 9.6 Degraded / outage visibility (Stage 9)
Interruption visible on screen throughout a drop + recovery, not just a silent amber dot.

### 9.7 Clips — hold-to-record, built ✅
Hold **Button A** to save the moment to the server for later review (dashboard). Nothing is stored on
the device (no storage beyond NVS config).

- **Flow:** hold BtnA → full-screen **recording** overlay (pause-screen style) + live `M:SS` timer,
  screen kept awake → release → **"saving…"** while it waits a short grace (no new utterance ~1.2 s,
  or a ~4 s cap) for the tail utterance to finish → sends the save → full-screen **saved / empty /
  save failed** status for ~2.5 s → resumes the glance.
- **What it sends (additive wire frame):** `{"type":"save","from":P+1,"to":<highest utterance id>}` —
  an inclusive **id range** of every utterance heard during the hold. Ids are tracked from **both**
  `utterance` and `read` frames, so the listener's own `user` lines (which get no `read`) and the
  tail are captured — all spoken text. A one-utterance clip is `from == to`.
- **Reply:** `{"type":"saved","id":n,"ok":<bool>[,"error":..][,"clip":..]}` drives the status. The
  device tolerates the reply being absent (optimistic confirm).
- **Code:** `net::saveClip(from,to)`, `proto::Type::Saved`, `emtext.ino` (hold start / release grace),
  `display::setClipRec` / `setSaving` / `setClip` (the three overlays).
- **Server:** retains a bounded per-connection ring (last 8 utterances / 45 s), bundles the id range
  into one `wav` + JSON sidecar, serves them under `/api/clips` on the dashboard — file-based,
  user-initiated retention (details in `CLAUDE.md`). The on-screen confirmation is the visible-
  retention signal (compliance: no third-party audio retained by default).

---

## 10. Decisions & open questions

**Decided:**
- ✅ **On-device audio output removed** — the pendant has no speaker: no tone cues, no
   alert/boot/low-battery tones, no audible idle warning, and no mute. The emotional signal is
   **visual only** (the colour of the glance heart, §6). The `cues` module (Stage 7) is dropped; the
   Settings mute row and mute glyph go with it. BtnA-hold is used for clips.
- ✅ **Clips are stored on the server, reviewed later** (not kept on-device). See §9.7 for the
   flow and wire-protocol addition.
- ✅ **Clip payload settled — an inclusive id range, no audio re-sent.** The device sends
   `{"type":"save","from":F,"to":T}`; the server concatenates the retained audio for those
   utterances into one `wav` with a JSON sidecar (§9.7).
- ✅ **Transcript shipped in More Info** — the dim second line under each read (§4.4).

**Still open:**
1. ~~**Clip payload:** id vs. audio?~~ **DECIDED** — id range, see above.
2. **Read-while-Dark:** wake on every read, only non-neutral, or cue-only?
3. ~~**Tone symbol:** keep the mark-as-hero curve, or revise the symbol set?~~ **DECIDED** — the
   Undertale heart, coloured by Undertale soul colours, pure colour-only (§6).
4. ~~**Transcript:** show it in More Info, or drop it?~~ **DECIDED / shipped** — More Info (§4.4).
5. **Settings back/idle:** explicit back (BtnB-hold/BtnA-hold→Glance, PWR→Dark) plus an idle
   timeout? *(Both exist now — explicit back and the 20 s `IDLE_MS` dim; open only if the
   timings want tuning.)*

*Keep this doc in sync when display/controls behavior changes — it's the single place to re-read
the whole device UX.*

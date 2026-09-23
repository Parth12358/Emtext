// A straight port of server/confidence.py with the shipped config values
// (server/config.py, 2026-09-22), so the site's confidence explorer and the
// pipeline simulation compute exactly what the server would. Keep the numbers
// in sync with config.py when they change; nothing here is site-only.

export const CONFIG = {
  PLATT_A: 1.0,
  PLATT_B: 0.0,
  HIGH: 0.9,
  LOW: 0.6,
  PENALTY: 0.7,
  MIN_VOICE_S: 1.5,
  MIN_ASR: 0.35,
  SER_MIN: 0.4,
}

export const REASONS = {
  offline: 'interpreter offline',
  noProb: 'interpreter gave no probability',
  short: 'too short to read the voice',
  voice: 'voice unclear',
  words: 'words may be misheard',
  twoVoices: 'two voices in one line',
  disagree: 'signals disagree',
}

export function platt(p, a = CONFIG.PLATT_A, b = CONFIG.PLATT_B) {
  const q = Math.min(Math.max(p, 1e-6), 1 - 1e-6)
  const z = a * Math.log(q / (1 - q)) + b
  return 1 / (1 + Math.exp(-z))
}

export function level(s) {
  if (s >= CONFIG.HIGH) return 'high'
  if (s < CONFIG.LOW) return 'low'
  return 'medium'
}

/** Same signature as confidence.score(); every input optional. */
export function score({ tone, toneProb, offline = false, asrConf, serConf, voicedS, speakerLabel }) {
  if (offline) return { score: 0, level: 'low', reasons: [REASONS.offline] }
  const reasons = []
  let s
  if (typeof toneProb === 'number' && toneProb >= 0 && toneProb <= 1) {
    s = platt(toneProb)
  } else {
    s = 0.5
    reasons.push(REASONS.noProb)
  }
  const pen = (r) => {
    s *= CONFIG.PENALTY
    reasons.push(r)
  }
  if (typeof voicedS === 'number' && voicedS < CONFIG.MIN_VOICE_S) pen(REASONS.short)
  if (typeof serConf === 'number' && serConf < CONFIG.SER_MIN) pen(REASONS.voice)
  if (typeof asrConf === 'number' && asrConf < CONFIG.MIN_ASR) pen(REASONS.words)
  if (speakerLabel === 'mixed') pen(REASONS.twoVoices)
  if (tone === 'mixed') pen(REASONS.disagree)
  s = Math.min(1, Math.max(0, s))
  return { score: Math.round(s * 1000) / 1000, level: level(s), reasons }
}

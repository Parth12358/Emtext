// Tiny DOM helpers shared by every section. No framework, no dependencies.

/** Build a single element from an HTML string. */
export function fromHTML(str) {
  const t = document.createElement('template')
  t.innerHTML = str.trim()
  return t.content.firstElementChild
}

/** Canonical tone metadata used across sections. */
export const TONES = {
  positive:  { label: 'positive',  color: 'var(--positive)' },
  negative:  { label: 'negative',  color: 'var(--negative)' },
  neutral:   { label: 'neutral',   color: 'var(--neutral)' },
  sarcastic: { label: 'sarcastic', color: 'var(--sarcastic)' },
  mixed:     { label: 'mixed',     color: 'var(--mixed)' },
}

# emtext docs

Everything except the two root files lives here. [`../README.md`](../README.md) is the landing
page and the server's user documentation (setup, wire protocol, config, evals, deployment);
[`../CLAUDE.md`](../CLAUDE.md) is the working agreement for anyone (human or model) editing the
code: module boundaries, invariants, and where the measurements behind each decision live.

| kind | doc | what it is, when to read it |
|---|---|---|
| status | [`status/TODO.md`](status/TODO.md) | Open issues **with the measurements behind them**. Read before touching the segmenter, the VAD knobs, SER, or the interpreter prompt; add to it rather than re-deriving. |
| design | [`design/RESEARCH.md`](design/RESEARCH.md) | Literature review behind the product: sarcasm/prosody findings, acoustic valence, diarization, licences. The "why" for most of TODO.md. |
| design | [`design/firmware-ui-ux.md`](design/firmware-ui-ux.md) | The pendant's screens, controls, state machine and dev glyphs, with what is built vs planned. |
| design | [`design/firmware-requirements.md`](design/firmware-requirements.md) | The firmware requirements the device is built against, with recorded deviations. |
| design | [`design/website-design-brief.md`](design/website-design-brief.md) | The brief the project site was built to. |
| guide | [`guides/firmware.md`](guides/firmware.md) | Building and flashing the M5StickS3 pendant: modules, stages, the wire contract from the device side, TLS debt. |
| guide | [`guides/tunnel.md`](guides/tunnel.md) | Exposing the server through a Cloudflare quick tunnel: token, scripts, keepalives, troubleshooting. |
| guide | [`guides/website.md`](guides/website.md) | The Vite site: layout, sections, dependencies. |
| report | [`reports/laya.md`](reports/laya.md) | Measured and rejected: a typed classifier (Laya) between SER and the LLM. Kept so the next candidate is scored the same way. |

Conventions: `status/` is the one file that changes with every finding; `design/` holds
intent and evidence; `guides/` holds how-to; `reports/` holds one-off measurements whose
conclusion is already in TODO.md. Each folder that lost its README (`firmware/`, `tunnel/`,
`website/`) keeps a three-line stub pointing here.

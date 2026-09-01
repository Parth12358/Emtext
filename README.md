# emtext

A live conversation interpreter. It transcribes speech as you hear it and, for
each sentence, adds a one-line read of the *emotional subtext* — is this
positive, negative, sarcastic, passive-aggressive, or just literal? It's built
for a neurodivergent listener who wants tone made explicit.

A browser page streams your microphone to a local server over a websocket. The
server slices the audio into utterances, transcribes each with Whisper (on
CPU), and asks a local LLM (via Ollama, on the GPU) what the line really means.
Later, an ESP32 device will replace the browser using the exact same wire
protocol.

```
mic ─► browser (AudioWorklet, 16k int16) ─websocket─► server
                                                        ├─ segmenter  (VAD)
                                                        ├─ transcriber (faster-whisper, CPU)
                                                        └─ interpreter (Ollama LLM, GPU)
```

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running locally
- A microphone, opened from **localhost or over HTTPS** (browsers only allow
  `getUserMedia` on secure origins — `http://localhost:8000` counts as secure)

## Setup

```bash
cd emtext
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# pull the interpreter model (default; override with OLLAMA_MODEL)
ollama pull gemma3:12b
```

The first run also downloads the Whisper model (`base` by default), which is
cached after that.

## Run

Start Ollama (if it isn't already running as a service):

```bash
ollama serve
```

Start the server from the `emtext/` directory:

```bash
python -m server.main
```

Then open <http://localhost:8000/?token=dev>, press **Start**, allow microphone
access, and talk. Transcripts appear newest-first; a tone-colored read shows up
beneath each one once the LLM responds.

- Green = positive · Red = negative · Grey = neutral · Purple = sarcastic ·
  Amber = mixed
- If Ollama is down, transcripts still appear with the read
  `(interpreter offline)` — the app degrades instead of breaking.

## Wire protocol

The browser client and the future ESP32 firmware both speak this:

1. Connect to `ws://host:8000/stream`.
2. Send **one TEXT frame**: the auth token. (Any value is accepted unless the
   `AUTH_TOKEN` env var is set on the server.)
3. Then send **BINARY frames**: raw PCM, 16 kHz, mono, int16 little-endian.

The server replies with TEXT JSON frames:

```jsonc
{"type":"ready"}
{"type":"status","state":"listening|heard|thinking"}
{"type":"utterance","id":1,"transcript":"..."}
{"type":"read","id":1,"tone":"positive|negative|neutral|sarcastic|mixed","read":"one line"}
```

## Tuning the VAD

The segmenter decides where sentences start and stop using simple frame energy.
Every knob is in `server/config.py` and can be overridden with an environment
variable of the same name. The ones you'll actually touch:

| Env var             | Default | What it does |
|---------------------|---------|--------------|
| `SPEECH_RMS`        | 500     | Loudness (int16 RMS) needed to count a frame as speech. **Raise it** if a noisy room keeps triggering; **lower it** if quiet speech gets missed. |
| `END_SILENCE_MS`    | 650     | How much trailing quiet ends an utterance. Lower = snappier but may split sentences at pauses; higher = fewer splits but more lag. |
| `MIN_UTTERANCE_MS`  | 350     | Utterances with less voiced audio than this are discarded as blips (coughs, taps). |
| `MAX_UTTERANCE_MS`  | 15000   | Hard cap; force-cuts run-on audio so the buffer never grows without bound. |
| `PRE_ROLL_MS`       | 240     | Audio kept from just *before* speech starts, so the first sound isn't clipped. |

Example — retune for a loud room and pull a bigger Whisper model:

```bash
SPEECH_RMS=900 WHISPER_MODEL=small python -m server.main
```

Check your tuning with the segmenter's built-in self-test (synthetic audio, no
mic needed):

```bash
python -m server.segmenter
```

Other useful env vars: `PORT`, `AUTH_TOKEN`, `OLLAMA_URL`, `OLLAMA_MODEL`,
`WHISPER_MODEL`, `CONTEXT_LINES`.

## Layout

```
server/
  main.py         FastAPI app + websocket route (wiring only)
  config.py       all tunables, env-overridable
  segmenter.py    pure VAD state machine: bytes in -> utterances out
  transcriber.py  faster-whisper wrapper (CPU)
  interpreter.py  Ollama wrapper + rolling conversation context
  static/index.html   browser test client (no build step)
eval/sarcasm_lines.txt  tricky lines to sanity-check the interpreter
experiments/      scratch space
firmware/         (future) ESP32 client
```

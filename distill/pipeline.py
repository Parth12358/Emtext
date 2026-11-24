"""Build the teacher-student distillation set, in resumable phases.

    python -m distill.pipeline generate --calls 40      # generator writes scenarios
    python -m distill.pipeline expand                   # scenarios -> rows (voice, tags, prompt)
    python -m distill.pipeline label                    # teacher qwen3:14b labels every row
    python -m distill.pipeline label --teacher gemma3:12b   # optional second teacher
    python -m distill.pipeline build                    # filter, dedup, split, export
    python -m distill.pipeline status                   # what exists so far

Everything lands in `data/distill/<name>/` (gitignored; `--name`, default the spec
version). Full guide: docs/guides/distill.md.

Shape, and why each phase is separate
-------------------------------------
1. **generate** -- an LLM (default `gemma3:12b`, a different family from the
   teacher, so the teacher is not grading prose in its own voice) writes short
   spoken exchanges, one category per call, each scenario pinned to a sampled
   setting, relationship, feeling and context length so the set does not
   converge on the generator's favourite small talk. Append-only
   `scenarios.jsonl`; re-running continues where it stopped.
2. **expand** -- pure and deterministic (seeded per scenario id). Each scenario
   becomes two rows under different voice variants (`distill.spec.VARIANTS`),
   with a voice hint from `distill.voice` and, for a quarter of rows, speaker
   tags. The model input is rendered by `Interpreter._build_prompt` ITSELF
   (design doc §5: "generate rows through that function rather than
   re-implementing it, so the training distribution cannot drift"). Rewrites
   `rows.jsonl`.
3. **label** -- the teacher answers every row through the real
   `Interpreter.interpret`, so the system prompt, schema, `think: false` and
   load options are production's byte for byte. Before each call the rendered
   prompt is re-checked against the stored one, so an interpreter edit between
   expand and label is caught instead of silently mislabelling. Append-only
   `labels_<model>.jsonl`; Ctrl+C is a clean stop and re-running resumes.
4. **build** -- keeps a row only when the teacher's tone AND emotion fall inside
   the rule-derived accepted sets (`distill.spec`), its read is at most 5 words
   and not a restatement of the line, the line is not a near-duplicate of
   another scenario or of an eval transcript, and its read string is under the
   diversity cap. Splits train/val BY SCENARIO (every variant of one line lands
   in the same split). Rejected rows go to `rejects.jsonl` with the reason.

Like `pipeline_eval`, every phase that loads a model evicts it on exit: the
interpreter pins with keep_alive:-1, and a model left resident makes the next
one spill into shared memory, which reads as "slow model".
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import sys
import time
import os
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from distill import spec, voice  # voice first: it switches SER off before server.config loads
from server import config
from server.interpreter import (EMOTIONS, SYSTEM_PROMPT, TONES, _EMOTION_SYNONYMS,
                                _TONE_SYNONYMS, Interpreter, _describe_voice, load_options)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "distill"

DEFAULT_GENERATOR = "gemma3:12b"
DEFAULT_TEACHER = "qwen3:14b"

SCENARIOS_PER_CALL = 6
# Earlier lines per scenario: mostly one or two, as in a real exchange; some
# with none, because the first line after a silence has no context either.
CONTEXT_LENGTHS = (0, 1, 1, 2, 2, 2, 3, 4)
GEN_TEMPERATURE = 0.9
# Six scenarios with up to four earlier lines each run ~1,000-1,500 tokens of
# JSON; 1,600 truncated an occasional call mid-object (JSONDecodeError).
GEN_NUM_PREDICT = 2400
VARIANTS_PER_SCENARIO = 2
VAL_SHARE = 0.05


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _seed(*parts: object) -> int:
    """A stable seed from any parts. zlib.crc32, not hash(): str hashing is salted per process."""
    return zlib.crc32("|".join(map(str, parts)).encode("utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # A half-written last line from a hard kill; everything before it is good.
                continue
    return out


def _append(fh, obj: dict) -> None:
    fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    fh.flush()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", text.lower())).strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _model_slug(model: str) -> str:
    return re.sub(r"[^\w.-]", "_", model)


def _evict(model: str) -> None:
    """Unload `model` (same load options as the interpreter, or Ollama reloads it). Never raises."""
    try:
        httpx.post(f"{config.OLLAMA_URL}/api/generate",
                   json={"model": model, "keep_alive": 0, "prompt": "", "stream": False,
                         "options": load_options()}, timeout=30)
        print(f"  evicted {model}", flush=True)
    except httpx.HTTPError as exc:
        print(f"  could not evict {model}: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Phase 1: generate
# ---------------------------------------------------------------------------

_GEN_SYSTEM = """You write short, realistic moments from spoken conversations. They are \
used to train a model that explains the emotional subtext of what someone says to a \
neurodivergent listener, so the lines must sound like real speech and the subtext must \
come from the situation, never from the speaker naming their own emotion."""


def _plan_call(call_index: int, base_seed: int) -> dict:
    """Everything one generator call is told, sampled from a per-call seed."""
    rng = random.Random(_seed(base_seed, "call", call_index))
    cat = spec.BY_KEY[spec.category_for_call(call_index)]
    items = []
    for i in range(SCENARIOS_PER_CALL):
        items.append({
            "setting": rng.choice(spec.SETTINGS),
            "relationship": rng.choice(spec.RELATIONSHIPS),
            # Cycle through the allowed feelings so they stay balanced within a category.
            "feeling": cat.feelings[(call_index * SCENARIOS_PER_CALL + i) % len(cat.feelings)],
            "context_lines": rng.choice(CONTEXT_LENGTHS),
        })
    return {"category": cat.key, "items": items}


def _gen_prompt(plan: dict) -> str:
    cat = spec.BY_KEY[plan["category"]]
    lines = [
        f"Write {len(plan['items'])} separate scenarios. In EVERY one, the newest line is: "
        f"{cat.brief}",
        "",
        "Two people: the LISTENER (the person being helped) and the SPEAKER (whose newest "
        "line will be interpreted). Earlier lines alternate between them, the LAST earlier "
        "line is always the listener's, and the newest line is the speaker's reply. Write "
        "earlier lines as plain strings, without speaker names.",
        "",
    ]
    for n, it in enumerate(plan["items"], 1):
        lines.append(f"scenario_{n}: {it['setting']}, between {it['relationship']}. "
                     f"The speaker actually feels {it['feeling']}. "
                     f"Exactly {it['context_lines']} earlier line(s).")
    lines += [
        "",
        "Rules:",
        "- If a setting and relationship do not fit together, keep the relationship and "
        "adapt the setting.",
        "- Spoken English as a speech recogniser would write it: contractions, sometimes "
        "a filler (um, like, I mean), and only commas, full stops, question marks and "
        "exclamation marks. No ellipses, dashes, stage directions, emoji, names or "
        "quotation marks inside a line.",
        "- Lines are short: 3 to 25 words. Vary length and wording between scenarios.",
        "- The speaker never names their own emotion (no \"I'm angry\", \"I'm being "
        "sarcastic\"). The situation carries it.",
        "- Do not use these phrases: " + "; ".join(spec.AVOID_PHRASES) + ".",
        "- Every scenario must be about something different.",
    ]
    return "\n".join(lines)


def _gen_schema(plan: dict) -> dict:
    """One keyed object per scenario, each with its EXACT context length and speakers.

    The first pilot (2026-10-25) asked for 411 earlier lines in prose and got 188:
    the generator ignores counts it is only told. A per-scenario minItems/maxItems
    makes the grammar enforce them. Keys, not an array, because Ollama rejects
    `prefixItems` ("schema must be an object"); keyed objects were measured to hit
    the requested counts exactly. Who says each line is not asked for at all: it is
    fixed by position in `_clean_scenario`.
    """
    def item(n: int) -> dict:
        return {"type": "object",
                "properties": {"context": {"type": "array", "items": {"type": "string"},
                                           "minItems": n, "maxItems": n},
                               "newest_line": {"type": "string"}},
                "required": ["context", "newest_line"]}

    keys = [f"scenario_{i + 1}" for i in range(len(plan["items"]))]
    return {"type": "object",
            "properties": {k: item(it["context_lines"]) for k, it in zip(keys, plan["items"])},
            "required": keys}


_ECHO = re.compile(
    r"\b(the )?(listener|speaker)\b|\bactually feels\b|\bearlier lines?\b|"
    r"\b(speaker|they) feels? (" + "|".join(spec.EMOTIONS) + r")\b|^\s*[(\[]|"
    r"\bvoice (cracks|shakes|trembles|breaks)\b", re.IGNORECASE)


def echo_problem(sc: dict) -> bool:
    """Did the generator copy its instructions or narration into the dialogue?

    Found in the 2026-10-25 pilot: 12 of 232 scenarios carried an "earlier line"
    like "a gym, between old school friends. The speaker actually feels neutral.
    Exactly 2 earlier lines." -- the plan text, TRUE FEELING INCLUDED, inside the
    model input. Trained on, the student learns to read the label off the prompt.
    Stage directions ("The listener's voice cracks") never reach a transcript either.
    """
    return any(_ECHO.search(t) for t in [sc["line"], *(c["line"] for c in sc["context"])])


def _clean_scenario(raw: dict, want_context: int) -> dict | None:
    """Validate one generated scenario; None drops it (recorded as a gen reject)."""
    newest = str(raw.get("newest_line", "")).strip().strip('"')
    if not (3 <= len(newest.split()) <= 30):
        return None
    lines = [str(c).strip().strip('"') for c in raw.get("context") or []]
    if len(lines) != want_context or any(not t or len(t.split()) > 40 for t in lines):
        return None
    # Earlier lines alternate and the last one is the LISTENER's, so the newest
    # (speaker) line is a reply to it -- the shape every real exchange has.
    n = len(lines)
    sc = {"context": [{"who": "listener" if (n - j) % 2 else "speaker", "line": t}
                      for j, t in enumerate(lines)],
          "line": newest}
    return None if echo_problem(sc) else sc


def generate(args: argparse.Namespace, out: Path) -> None:
    path = out / "scenarios.jsonl"
    # Only calls that produced scenarios count as done. A failed call (logged to
    # gen_rejects.jsonl) is retried on the next run: in the 2026-10-25 pilot they
    # were truncated JSON, a transient, not a property of the call's plan.
    existing = _read_jsonl(path)
    # Call -> category depends on the spec's weights, so resuming a folder made
    # under another spec would quietly mix two plans. Start a new --name instead.
    other = {s.get("spec") for s in existing} - {spec.SPEC_VERSION}
    if other:
        raise SystemExit(f"generate: {path} holds spec {sorted(map(str, other))} scenarios, "
                         f"this is spec {spec.SPEC_VERSION}; use a new --name")
    done_calls = {s["call"] for s in existing}
    todo = [i for i in range(args.calls) if i not in done_calls]
    print(f"generate: {len(done_calls)} calls done, {len(todo)} to go "
          f"({SCENARIOS_PER_CALL} scenarios each, generator {args.generator})", flush=True)
    if not todo:
        return
    t0 = time.perf_counter()
    try:
        with httpx.Client() as client, path.open("a", encoding="utf-8") as fh, \
                (out / "gen_rejects.jsonl").open("a", encoding="utf-8") as rej:
            for k, call in enumerate(todo, 1):
                plan = _plan_call(call, args.seed)
                payload = {
                    "model": args.generator, "system": _GEN_SYSTEM, "prompt": _gen_prompt(plan),
                    "stream": False, "format": _gen_schema(plan), "think": False,
                    "keep_alive": -1,
                    "options": {"temperature": GEN_TEMPERATURE, "num_predict": GEN_NUM_PREDICT,
                                "seed": _seed(args.seed, "gen", call), **load_options()},
                }
                try:
                    resp = client.post(f"{config.OLLAMA_URL}/api/generate", json=payload,
                                       timeout=300)
                    resp.raise_for_status()
                    body = json.loads(resp.json()["response"])
                    scenarios = [body[f"scenario_{i + 1}"] for i in range(len(plan["items"]))]
                except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError) as exc:
                    _append(rej, {"call": call, "error": f"{type(exc).__name__}: {exc}"})
                    print(f"  call {call}: failed ({type(exc).__name__})", flush=True)
                    continue
                kept = 0
                for i, (raw, it) in enumerate(zip(scenarios, plan["items"])):
                    clean = _clean_scenario(raw, it["context_lines"])
                    if clean is None:
                        continue
                    _append(fh, {"id": f"{plan['category']}-{call:05d}-{i}", "call": call,
                                 "category": plan["category"], **it, **clean,
                                 "generator": args.generator, "spec": spec.SPEC_VERSION})
                    kept += 1
                rate = (time.perf_counter() - t0) / k
                print(f"  [{k:>4}/{len(todo)}] call {call:>5} {plan['category']:<19} "
                      f"kept {kept}/{len(plan['items'])}  eta {rate * (len(todo) - k) / 60:5.1f}m",
                      flush=True)
    except KeyboardInterrupt:
        print("\n  stopped; re-run to continue", flush=True)
    finally:
        _evict(args.generator)


# ---------------------------------------------------------------------------
# Phase 2: expand (pure, deterministic)
# ---------------------------------------------------------------------------

_TYPOGRAPHY = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def asr_text(text: str) -> str:
    """Make generated prose look like what the speech recogniser hands the interpreter.

    LLM writers mark hesitation with "…", em dashes and curly quotes; parakeet and
    whisper emit none of them (commas and full stops only). Left in, they are a
    generator signature the student would learn as a cue that never reaches it in
    production (design doc §5.2). Applied in expand, so scenarios.jsonl keeps the
    raw generator text.
    """
    t = text.translate(_TYPOGRAPHY)
    t = re.sub(r"[*_]+(\w[^*_]*?)[*_]+", r"\1", t)  # markdown emphasis: *exactly*
    t = re.sub(r"\s*(…|\.{2,}|—|–|--)\s*", ", ", t)
    t = re.sub(r",\s*([,.?!])", r"\1", t)          # ", ." -> "."
    t = re.sub(r"^\s*,\s*", "", t)                  # a line that opened with "..."
    return re.sub(r"\s{2,}", " ", t).strip(" ,")


def _voice_label(choices: tuple[str, ...], feeling: str, rng: random.Random) -> str:
    label = rng.choice(choices)
    return feeling if label == "FEELING" else label


def expand_scenario(sc: dict) -> list[dict]:
    """One scenario -> VARIANTS_PER_SCENARIO rows. Same input, same rows, every time."""
    cat = spec.BY_KEY[sc["category"]]
    rng = random.Random(_seed("expand", spec.SPEC_VERSION, sc["id"]))
    sc = {**sc, "line": asr_text(sc["line"]),
          "context": [{**c, "line": asr_text(c["line"])} for c in sc["context"]]}
    allowed = list(cat.variants())
    weights = [cat.variant_weight(v) for v in allowed]
    picked: list[str] = []
    while len(picked) < min(VARIANTS_PER_SCENARIO, len(allowed)):
        v = rng.choices(allowed, weights=weights)[0]
        if v not in picked:
            picked.append(v)

    rows = []
    for variant in picked:
        if variant == "congruent":
            vdict = voice.sample(rng, _voice_label(cat.delivery, sc["feeling"], rng))
        elif variant == "contrast":
            vdict = voice.sample(rng, _voice_label(cat.contrast, sc["feeling"], rng))
        elif variant == "low_conf":
            vdict = voice.sample(rng, rng.choice(voice.EMOTIONS), low_confidence=True)
        else:
            vdict = None

        labelled = bool(sc["context"]) and rng.random() < spec.LABELLED_SHARE
        if labelled:
            ctx = [("user" if c["who"] == "listener" else "other", c["line"]) for c in sc["context"]]
            who = "mixed" if rng.random() < spec.MIXED_WHO_SHARE else "other"
        else:
            ctx = [(None, c["line"]) for c in sc["context"]]
            who = None

        interp = Interpreter(None)  # _build_prompt never touches the client
        for w, line in ctx:
            interp.remember(line, w)
        prompt = interp._build_prompt(sc["line"], _describe_voice(vdict), who)

        exp = spec.expectation(sc["category"], variant, (vdict or {}).get("emotion"),
                               sc["feeling"])
        rows.append({
            "id": f"{sc['id']}:{variant}", "scenario": sc["id"], "category": sc["category"],
            "variant": variant, "feeling": sc["feeling"], "line": sc["line"],
            "context": [[w, line] for w, line in ctx], "who": who, "voice": vdict,
            "expect_tone": exp[0], "expect_emotion": exp[1], "prompt": prompt,
        })
    return rows


def expand(args: argparse.Namespace, out: Path) -> None:
    scenarios = _read_jsonl(out / "scenarios.jsonl")
    # Variant picks are seeded by the spec version, so expanding another spec's
    # scenarios would rewrite every row and orphan its labels as stale_label.
    other = {sc.get("spec") for sc in scenarios} - {spec.SPEC_VERSION}
    if other:
        raise SystemExit(f"expand: scenarios are spec {sorted(map(str, other))}, this is "
                         f"spec {spec.SPEC_VERSION}; regenerate under a new --name")
    # Re-checked here, not only at generation, so a set generated before the
    # check existed is cleaned too.
    echoes = [sc for sc in scenarios if echo_problem(sc)]
    scenarios = [sc for sc in scenarios if not echo_problem(sc)]
    if echoes:
        print(f"expand: dropped {len(echoes)} scenarios that echo the generator's "
              "instructions or narration")
    rows = [r for sc in scenarios for r in expand_scenario(sc)]
    path = out / "rows.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            _append(fh, r)
    by = Counter((r["category"], r["variant"]) for r in rows)
    print(f"expand: {len(scenarios)} scenarios -> {len(rows)} rows "
          f"({sum(1 for r in rows if r['who'])} speaker-tagged) -> {path}")
    for cat in spec.CATEGORIES:
        cells = "  ".join(f"{v}:{by[(cat.key, v)]}" for v in spec.VARIANTS if by[(cat.key, v)])
        print(f"  {cat.key:<19} {cells}")


# ---------------------------------------------------------------------------
# Phase 3: label
# ---------------------------------------------------------------------------

async def _label_async(args: argparse.Namespace, out: Path) -> None:
    rows = _read_jsonl(out / "rows.jsonl")
    if not rows:
        raise SystemExit("no rows.jsonl yet: run generate, then expand")
    path = out / f"labels_{_model_slug(args.teacher)}.jsonl"
    done = {r["id"] for r in _read_jsonl(path)}
    todo = [r for r in rows if r["id"] not in done]
    print(f"label: teacher {args.teacher}, {len(done)} done, {len(todo)} to go", flush=True)
    if not todo:
        return

    config.OLLAMA_MODEL = args.teacher  # Interpreter reads it at call time
    sem = asyncio.Semaphore(max(1, args.concurrency))
    t0 = time.perf_counter()
    counter = {"n": 0, "offline": 0}

    async with httpx.AsyncClient() as client:
        await Interpreter(client).interpret("Warming up the model.")  # untimed load

        async def one(row: dict, fh) -> None:
            async with sem:
                interp = Interpreter(client)
                for w, line in row["context"]:
                    interp.remember(line, w)
                rendered = interp._build_prompt(row["line"], _describe_voice(row["voice"]),
                                                row["who"])
                if rendered != row["prompt"]:
                    raise SystemExit(f"{row['id']}: _build_prompt output changed since expand; "
                                     "re-run expand (and relabel) before labelling more")
                start = time.perf_counter()
                res = await interp.interpret(row["line"], row["voice"], row["who"])
                _append(fh, {"id": row["id"], "model": args.teacher,
                             "prompt_sha": _sha(row["prompt"]),
                             "emotion": res["emotion"], "tone": res["tone"], "read": res["read"],
                             "tone_prob": res.get("tone_prob"),
                             "emotion_prob": res.get("emotion_prob"),
                             "offline": res["offline"],
                             "elapsed_s": round(time.perf_counter() - start, 3)})
                counter["n"] += 1
                counter["offline"] += int(res["offline"])
                n = counter["n"]
                if n % 25 == 0 or n == len(todo):
                    rate = (time.perf_counter() - t0) / n
                    print(f"  [{n:>5}/{len(todo)}] {rate:.2f}s/row  offline {counter['offline']}"
                          f"  eta {rate * (len(todo) - n) / 60:5.1f}m", flush=True)

        with path.open("a", encoding="utf-8") as fh:
            batch = max(1, args.concurrency) * 4
            for i in range(0, len(todo), batch):
                await asyncio.gather(*(one(r, fh) for r in todo[i:i + batch]))


# A hosted teacher for the cells the local teachers get wrong (2026-11-23: the
# v2 build kept 65/585 warm-voice teasing and 62/413 sad-voice sincere-positive
# rows, because qwen3:14b and gemma3:12b both answer against the prompt's own
# rules there). OpenAI-compatible chat completions. The key is read from the
# environment only and never written to any file this pipeline produces.
API_PROVIDERS = {
    "deepseek": {"url": "https://api.deepseek.com/chat/completions", "key_env": "DEEPSEEK_API_KEY"},
}
API_MAX_TOKENS = 200            # the answer is ~30 tokens
API_THINKING_MAX_TOKENS = 4000  # thinking tokens count against max_tokens
API_RETRIES = 3


def _parse_api_answer(text: str) -> dict | None:
    """The teacher's JSON, normalised like Interpreter.interpret, or None if unusable.

    Unlike interpret(), an off-enum or missing emotion is a FAILED label, not a
    fallback to the voice label: there is no decode-time schema here (DeepSeek
    offers json_object mode only), and a silent "keep the voice" would pass the
    sincere-positive/contrast gate, whose accepted emotion IS the voice label.
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    tone = str(parsed.get("tone", "")).lower().strip()
    tone = _TONE_SYNONYMS.get(tone, tone)
    emotion = str(parsed.get("emotion", "")).lower().strip()
    emotion = _EMOTION_SYNONYMS.get(emotion, emotion)
    read = str(parsed.get("read", "")).strip()
    if tone not in TONES or emotion not in EMOTIONS or not read:
        return None
    return {"emotion": emotion, "tone": tone, "read": read}


async def _label_api_async(args: argparse.Namespace, out: Path) -> None:
    prov = API_PROVIDERS[args.api]
    key = os.environ.get(prov["key_env"], "").strip()
    if not key:
        raise SystemExit(f"label --api {args.api}: set {prov['key_env']}")
    rows = _read_jsonl(out / "rows.jsonl")
    if args.cells:
        rows = [r for r in rows if f"{r['category']}/{r['variant']}" in args.cells]
    if args.limit:
        rows = rows[:args.limit]
    path = out / f"labels_{_model_slug(args.teacher)}.jsonl"
    done = {r["id"] for r in _read_jsonl(path)}
    todo = [r for r in rows if r["id"] not in done]
    print(f"label: teacher {args.teacher} via {args.api} "
          f"(thinking {'on' if args.api_thinking else 'off'}), {len(done)} done, "
          f"{len(todo)} to go", flush=True)
    if not todo:
        return

    sem = asyncio.Semaphore(max(1, args.concurrency))
    t0 = time.perf_counter()
    tally = Counter()

    async def ask(client: httpx.AsyncClient, row: dict) -> tuple[dict | None, dict, str]:
        body = {
            "model": args.teacher,
            # The same two inputs production sends to Ollama, as chat messages.
            # SYSTEM_PROMPT already says "JSON" and spells out the object, which
            # is what json_object mode asks the prompt to contain.
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": row["prompt"]}],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "enabled" if args.api_thinking else "disabled"},
            "max_tokens": API_THINKING_MAX_TOKENS if args.api_thinking else API_MAX_TOKENS,
            "temperature": config.OLLAMA_TEMPERATURE,  # ignored in thinking mode
        }
        last = ""
        for attempt in range(API_RETRIES):
            try:
                resp = await client.post(prov["url"], json=body, timeout=180,
                                         headers={"Authorization": f"Bearer {key}"})
                if resp.status_code == 429 or resp.status_code >= 500:
                    last = f"HTTP {resp.status_code}"
                    await asyncio.sleep(2 ** attempt * 2)
                    continue
                if resp.status_code >= 400:
                    # Bad request / auth: retrying cannot help. Body, not headers,
                    # so the key never reaches the log.
                    return None, {}, f"HTTP {resp.status_code}: {resp.text[:200]}"
                data = resp.json()
                text = data["choices"][0]["message"].get("content") or ""
                # json_object mode blanks invalid JSON to spaces rather than erroring.
                ans = _parse_api_answer(text)
                if ans:
                    return ans, data, ""
                last = f"unusable answer: {text.strip()[:120]!r}"
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                last = f"{type(exc).__name__}: {str(exc)[:120]}"
                await asyncio.sleep(2 ** attempt)
        return None, {}, last

    async def one(client: httpx.AsyncClient, row: dict, fh) -> None:
        async with sem:
            start = time.perf_counter()
            ans, data, err = await ask(client, row)
            usage = data.get("usage") or {}
            for k in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "completion_tokens"):
                tally[k] += int(usage.get(k) or 0)
            tally["n"] += 1
            tally["failed"] += int(ans is None)
            _append(fh, {"id": row["id"], "model": args.teacher, "api": args.api,
                         "served_model": data.get("model"), "thinking": args.api_thinking,
                         "prompt_sha": _sha(row["prompt"]),
                         **(ans or {"emotion": None, "tone": None, "read": "(teacher failed)"}),
                         "tone_prob": None, "emotion_prob": None,  # no logprobs requested
                         "offline": ans is None, "error": err or None,
                         "elapsed_s": round(time.perf_counter() - start, 3)})
            n = tally["n"]
            if n % 25 == 0 or n == len(todo):
                rate = (time.perf_counter() - t0) / n
                tin = tally["prompt_cache_hit_tokens"] + tally["prompt_cache_miss_tokens"]
                print(f"  [{n:>5}/{len(todo)}] {rate:.2f}s/row  failed {tally['failed']}  "
                      f"tokens in {tin:,} (cached {tally['prompt_cache_hit_tokens']:,}) "
                      f"out {tally['completion_tokens']:,}  eta {rate * (len(todo) - n) / 60:5.1f}m",
                      flush=True)

    async with httpx.AsyncClient() as client:
        with path.open("a", encoding="utf-8") as fh:
            await asyncio.gather(*(one(client, r, fh) for r in todo))


def label(args: argparse.Namespace, out: Path) -> None:
    if args.api:
        try:
            asyncio.run(_label_api_async(args, out))
        except KeyboardInterrupt:
            print("\n  stopped; re-run to continue", flush=True)
        return
    try:
        asyncio.run(_label_async(args, out))
    except KeyboardInterrupt:
        print("\n  stopped; re-run to continue", flush=True)
    finally:
        _evict(args.teacher)


# ---------------------------------------------------------------------------
# Phase 4: build
# ---------------------------------------------------------------------------

def _shingles(text: str, k: int = 4) -> set[str]:
    t = _norm(text)
    return {t[i:i + k] for i in range(max(1, len(t) - k + 1))}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _near_duplicates(lines: dict[str, str], threshold: float) -> set[str]:
    """Scenario ids whose line is a near-duplicate of an EARLIER scenario's line.

    MinHash LSH over character 4-grams (32 hashes, 8 bands of 4): candidates
    share a band, and only candidates are compared exactly. Pure Python + zlib,
    so no new dependency; a few thousand lines take well under a second.
    """
    p = (1 << 61) - 1
    rng = random.Random(1234)
    perms = [(rng.randrange(1, p), rng.randrange(0, p)) for _ in range(32)]
    sh = {sid: _shingles(t) for sid, t in lines.items()}
    buckets: dict[tuple, list[str]] = defaultdict(list)
    for sid, s in sh.items():
        hs = [zlib.crc32(g.encode()) for g in s]
        sig = [min((a * h + b) % p for h in hs) for a, b in perms]
        for band in range(8):
            buckets[(band, *sig[band * 4:band * 4 + 4])].append(sid)
    dup: set[str] = set()
    order = {sid: i for i, sid in enumerate(lines)}
    for members in buckets.values():
        if len(members) < 2:
            continue
        members = sorted(set(members), key=order.get)
        for i, later in enumerate(members):
            if later in dup:
                continue
            if any(_jaccard(sh[earlier], sh[later]) >= threshold
                   for earlier in members[:i] if earlier not in dup):
                dup.add(later)
    return dup


_STOP = frozenset("""a an the i you he she it we they me him her us them my your our their
is am are was were be been do does did have has had will would can could should to of in
on at for with and or but so not no that this just really like um oh well yeah okay""".split())


def _content_trigrams(text: str) -> set[tuple[str, ...]]:
    """Word 3-grams with at least one non-filler word.

    Character Jaccard alone missed the first pilot's copies: "Thanks for finally
    noticing the fridge" vs pa-03 "Thanks for finally getting back to me" share
    the telling run but little else. A shared content 3-gram is the copy signal;
    all-filler runs ("I don't know") are too common to mean anything.
    """
    w = _norm(text).split()
    # Two content words, not one: "you know that" (one) is everyday speech and
    # rejected an innocent pilot line; "thanks for finally" (two) is the copy.
    return {g for g in zip(w, w[1:], w[2:]) if sum(x not in _STOP for x in g) >= 2}


def _eval_lines() -> list[str]:
    """Every transcript and context line in the eval suites, which must stay held out (§5.5)."""
    from eval.ser_eval import STATEMENTS

    lines = list(STATEMENTS.values())
    for raw in (ROOT / "eval" / "tone_cases.jsonl").read_text(encoding="utf-8").splitlines():
        if raw.strip():
            obj = json.loads(raw)
            if "transcript" in obj:
                lines.append(obj["transcript"])
                lines.extend(obj.get("context") or [])
    return lines


def normalise_read(read: str) -> str:
    """One house style for every target read: lowercase, comma-joined, no end stop.

    The two teachers write differently: qwen3:14b "masking hurt" (0 of 464 pilot
    reads capitalised or punctuated), gemma3:12b "Information. Schedule. Be
    there." (435 capitalised, 392 with a full stop). Mixed, the student learns
    two output styles. This is the 14B's style, which is also SYSTEM_PROMPT's
    examples ("masking hurt", "friendly teasing").
    """
    pieces = [p.strip() for p in re.split(r"[.;:!]+(?:\s+|$)", read.strip())]
    # Lowercase the first letter of each sentence piece ("Information. Schedule."
    # -> "information, schedule"), but never the word "I".
    pieces = [p[0].lower() + p[1:] if p[:1].isupper() and not re.match(r"I\b", p) else p
              for p in pieces if p]
    return ", ".join(pieces).strip(" ,")


def _read_problem(read: str, line: str) -> str | None:
    words = read.split()
    if not words or read in ("(no read)", "(interpreter offline)"):
        return "no_read"
    if len(words) > spec.READ_MAX_WORDS:
        return "read_too_long"
    nr = _norm(read)
    if len(nr.split()) >= 3 and nr in _norm(line):
        return "read_restates_line"
    return None


def build(args: argparse.Namespace, out: Path) -> None:
    rows = _read_jsonl(out / "rows.jsonl")
    t1 = {r["id"]: r for r in _read_jsonl(out / f"labels_{_model_slug(args.teacher)}.jsonl")}
    t2 = ({r["id"]: r for r in _read_jsonl(out / f"labels_{_model_slug(args.second)}.jsonl")}
          if args.second else {})
    # Further fallbacks after --second, tried in order (e.g. a hosted teacher
    # that labelled only the weak cells). Same rule gate; never used for agreement.
    extra = [(m, {r["id"]: r for r in _read_jsonl(out / f"labels_{_model_slug(m)}.jsonl")})
             for m in args.also]
    if not rows or not t1:
        raise SystemExit("need rows.jsonl and the teacher's labels: run expand and label first")

    # Scenario-level filters first: a duplicated or leaked LINE removes every
    # variant of it, since the variants differ only in voice.
    scen_line = {}
    for r in rows:
        scen_line.setdefault(r["scenario"], r["line"])
    dups = _near_duplicates(scen_line, args.dup_threshold)
    eval_lines = _eval_lines()
    evals = [_shingles(x) for x in eval_lines]
    eval_grams = set().union(*(_content_trigrams(x) for x in eval_lines))
    avoid = [_norm(a) for a in spec.AVOID_PHRASES]
    leaks = {sid for sid, line in scen_line.items()
             if any(_jaccard(_shingles(line), e) >= args.leak_threshold for e in evals)
             or _content_trigrams(line) & eval_grams
             or any(a in _norm(line) for a in avoid)}

    def label_problem(r: dict, lab: dict | None) -> str | None:
        """Why one teacher's answer cannot be this row's target, or None."""
        if lab is None:
            return "unlabelled"
        if lab.get("prompt_sha") and lab["prompt_sha"] != _sha(r["prompt"]):
            # rows.jsonl was re-expanded with different input since labelling;
            # this answer was given to a prompt the row no longer has.
            return "stale_label"
        if lab["offline"]:
            return "offline"
        if lab["tone"] not in r["expect_tone"]:
            return "tone_outside_rules"
        if lab["emotion"] not in r["expect_emotion"]:
            return "emotion_outside_rules"
        return _read_problem(lab["read"], r["line"])

    kept, rejects = [], []
    reasons = Counter()
    for r in rows:
        first, second = t1.get(r["id"]), t2.get(r["id"])
        if r["scenario"] in leaks:
            why, lab, source = "eval_leak", first, args.teacher
        elif r["scenario"] in dups:
            why, lab, source = "near_duplicate", first, args.teacher
        else:
            why, lab, source = label_problem(r, first), first, args.teacher
            # Rule-gated fallback: when the primary teacher's answer breaks the
            # prompt's own rules but the second teacher's satisfies them, the row
            # is kept with the second's label. Found in the 2026-10-25 pilot:
            # qwen3:14b reads warm-voice teasing as negative/angry (its known
            # mm-01a/mm-03a failure), leaving 2 of 24 such rows. The rules, not
            # the second teacher, still decide what is acceptable, so gemma's
            # own known failure (the low-confidence guard) cannot leak in.
            fixable = ("tone_outside_rules", "emotion_outside_rules", "read_too_long",
                       "read_restates_line", "no_read")
            for model, labs in ([(args.second, t2)] if args.second else []) + extra:
                if not why or args.no_fallback or why not in fixable:
                    break
                cand = labs.get(r["id"])
                if cand and label_problem(r, cand) is None:
                    why, lab, source = None, cand, model
        agree = bool(first and second and not first["offline"] and not second["offline"]
                     and second["tone"] == first["tone"]
                     and second["emotion"] == first["emotion"])
        if why is None and args.require_agree and not agree:
            why = "teachers_disagree"
        rec = {"row": r, "label": lab, "second": second, "agree": agree, "source": source}
        if why:
            reasons[why] += 1
            rejects.append({**rec, "reason": why})
        else:
            kept.append(rec)

    # Read diversity cap, applied in a seeded order so it does not always keep
    # the earliest-generated categories.
    rng = random.Random(_seed("cap", args.seed))
    rng.shuffle(kept)
    cap = max(spec.READ_CAP_MIN, int(spec.READ_CAP_SHARE * len(kept)))
    seen = Counter()
    final = []
    for rec in kept:
        key = _norm(normalise_read(rec["label"]["read"]))
        if seen[key] >= cap:
            reasons["read_cap"] += 1
            rejects.append({**rec, "reason": "read_cap"})
            continue
        seen[key] += 1
        final.append(rec)
    final.sort(key=lambda rec: rec["row"]["id"])

    def split_of(scenario: str) -> str:
        return "val" if (_seed("split", args.seed, scenario) % 10_000) / 10_000 < VAL_SHARE else "train"

    files = {s: (out / f"{s}.jsonl").open("w", encoding="utf-8") for s in ("train", "val")}
    metas = {s: (out / f"{s}.meta.jsonl").open("w", encoding="utf-8") for s in ("train", "val")}
    counts = Counter()
    try:
        for rec in final:
            r, lab = rec["row"], rec["label"]
            s = split_of(r["scenario"])
            target = json.dumps({"emotion": lab["emotion"], "tone": lab["tone"],
                                 "read": normalise_read(lab["read"])}, ensure_ascii=False)
            # TRL's conversational prompt/completion format: loss lands on the
            # completion only, with no {% generation %} tags needed (design §6.2).
            _append(files[s], {
                "prompt": [{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user", "content": r["prompt"]}],
                "completion": [{"role": "assistant", "content": target}],
            })
            _append(metas[s], {
                "id": r["id"], "category": r["category"], "variant": r["variant"],
                "feeling": r["feeling"], "who": r["who"], "voice": r["voice"],
                "expect_tone": r["expect_tone"], "expect_emotion": r["expect_emotion"],
                "tone_prob": lab.get("tone_prob"), "emotion_prob": lab.get("emotion_prob"),
                "second": ({k: rec["second"][k] for k in ("model", "emotion", "tone", "read")}
                           if rec["second"] else None),
                "teachers_agree": rec["agree"], "label_source": rec["source"],
            })
            counts[(s, r["category"], r["variant"])] += 1
    finally:
        for fh in (*files.values(), *metas.values()):
            fh.close()
    with (out / "rejects.jsonl").open("w", encoding="utf-8") as fh:
        for rec in rejects:
            _append(fh, {"id": rec["row"]["id"], "reason": rec["reason"],
                         "category": rec["row"]["category"], "variant": rec["row"]["variant"],
                         "line": rec["row"]["line"], "voice": rec["row"]["voice"],
                         "expect_tone": rec["row"]["expect_tone"],
                         "expect_emotion": rec["row"]["expect_emotion"],
                         "got": ({k: rec["label"][k] for k in ("emotion", "tone", "read")}
                                 if rec["label"] else None)})

    per_cell = defaultdict(lambda: {"rows": 0, "kept": 0})
    for r in rows:
        per_cell[f"{r['category']}/{r['variant']}"]["rows"] += 1
    for rec in final:
        per_cell[f"{rec['row']['category']}/{rec['row']['variant']}"]["kept"] += 1
    manifest = {
        "name": out.name, "spec": spec.SPEC_VERSION,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "generator": sorted({s.get("generator") for s in _read_jsonl(out / "scenarios.jsonl")}),
        "teacher": args.teacher, "second_teacher": args.second, "fallback_teachers": args.also,
        "require_agree": args.require_agree,
        "rows_in": len(rows), "kept": len(final),
        "train": sum(v for (s, *_), v in counts.items() if s == "train"),
        "val": sum(v for (s, *_), v in counts.items() if s == "val"),
        "reject_reasons": dict(reasons.most_common()),
        "distinct_reads": len(seen), "read_cap": cap,
        "label_source": dict(Counter(rec["source"] for rec in final)),
        "teachers_agree_share": (round(sum(r["agree"] for r in final) / len(final), 3)
                                 if final and t2 else None),
        "per_cell": dict(sorted(per_cell.items())),
        "voice_confidence": {"confident": voice.CONFIDENT, "low": voice.LOW_CONF},
        "decisions": ["english only", "emotion2vec-shaped hints only (no valence)",
                      "no public corpora seeded yet"],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"build: {len(rows)} rows -> kept {len(final)} "
          f"(train {manifest['train']}, val {manifest['val']}), {len(seen)} distinct reads")
    print("  rejected: " + ", ".join(f"{k} {v}" for k, v in reasons.most_common()))
    if t2:
        print(f"  second teacher {args.second} agrees on {manifest['teachers_agree_share']:.0%} "
              f"of kept rows; label source {manifest['label_source']}")
    print(f"  {'cell':<32}{'rows':>6}{'kept':>6}")
    for cell, c in manifest["per_cell"].items():
        print(f"  {cell:<32}{c['rows']:>6}{c['kept']:>6}  {100 * c['kept'] / c['rows']:>4.0f}%")
    print(f"  -> {out}")


# ---------------------------------------------------------------------------

def status(args: argparse.Namespace, out: Path) -> None:
    print(f"{out}")
    for name in sorted(p.name for p in out.glob("*.jsonl")) + ["manifest.json"]:
        p = out / name
        if p.exists():
            n = sum(1 for _ in p.open(encoding="utf-8")) if p.suffix == ".jsonl" else "-"
            print(f"  {name:<34}{n:>8}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=("generate", "expand", "label", "build", "status"))
    ap.add_argument("--name", default=spec.SPEC_VERSION,
                    help="dataset folder under data/distill/ (default: the spec version)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--calls", type=int, default=40,
                    help=f"generate: total generator calls ({SCENARIOS_PER_CALL} scenarios each)")
    ap.add_argument("--generator", default=DEFAULT_GENERATOR)
    ap.add_argument("--teacher", default=DEFAULT_TEACHER,
                    help="label: the model to label with; build: the primary teacher")
    ap.add_argument("--second", default=None, metavar="MODEL",
                    help="build: a second teacher whose labels exist, for agreement")
    ap.add_argument("--also", action="append", default=[], metavar="MODEL",
                    help="build: a further fallback teacher, tried after --second "
                         "(repeatable; same rule gate)")
    ap.add_argument("--api", choices=sorted(API_PROVIDERS), default=None,
                    help="label: send --teacher to a hosted API instead of Ollama")
    ap.add_argument("--api-thinking", action="store_true",
                    help="label --api: let the model think before answering")
    ap.add_argument("--cells", type=lambda v: set(v.split(",")), default=None,
                    help="label: only rows in these category/variant cells (comma list)")
    ap.add_argument("--limit", type=int, default=0,
                    help="label: only the first N matching rows (a trial)")
    ap.add_argument("--require-agree", action="store_true",
                    help="build: also drop rows where the second teacher disagrees")
    ap.add_argument("--no-fallback", action="store_true",
                    help="build: never use the second teacher's label when the primary's "
                         "breaks the rules (default: use it if it satisfies them)")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="label: parallel requests (Ollama: helps only with "
                         "OLLAMA_NUM_PARALLEL > 1; --api: try 8)")
    ap.add_argument("--dup-threshold", type=float, default=0.7,
                    help="build: char-4-gram Jaccard at which two lines are one scenario")
    ap.add_argument("--leak-threshold", type=float, default=0.5,
                    help="build: Jaccard to any eval transcript at which a line is rejected")
    args = ap.parse_args()

    out = DATA / args.name
    out.mkdir(parents=True, exist_ok=True)
    {"generate": generate, "expand": expand, "label": label, "build": build,
     "status": status}[args.phase](args, out)


if __name__ == "__main__":
    sys.exit(main())

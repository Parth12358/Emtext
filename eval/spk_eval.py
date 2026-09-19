"""Score speaker identification (server/speaker.py) against RAVDESS.

    python -m eval.spk_eval                 # everything below, 24 actors
    python -m eval.spk_eval --eer           # verification EER, calm vs spread enrolment
    python -m eval.spk_eval --pairs 120     # nearest-centroid on random actor pairs
    python -m eval.spk_eval --label         # 3-way user/other/mixed on glued turns
    python -m eval.spk_eval --real me.wav conv.wav [--partner them.wav]

Why RAVDESS again
-----------------
Every actor speaks the *same two sentences*, so lexical content is controlled
and any separation the embedding finds is speaker identity, not words. It is
also acted across eight emotions, which is the adversarial case that matters
here: a voiceprint enrolled on calm speech must still recognise the same person
when they are upset, because that is when this app is needed.

What it is not: a real room. One voice per file, studio mic, no reverb and no
overlap. Every number here is a ceiling, and `--real` exists so a real
two-person recording can be scored the moment one exists.

The three checks map onto the three claims in TODO.md ("No diarization"):

  --eer    verification, one clip vs one profile. Shows the enrolment-spread
           effect (calm-only ~14.8% EER vs all-emotion ~11.8%).
  --pairs  the actual decision rule: user vs an online partner centroid,
           on interleaved turns from two actors (~94% with spread enrolment).
  --label  the glued-utterance case: an utterance holding a turn change gets
           labelled `mixed` from its window fraction rather than cut. The number
           to watch is "partner labelled as user" -- a partner line silently
           dropped -- which should stay near 1%.

Clips are silence-trimmed at SPEECH_RMS=150 first (RAVDESS is much quieter than
a live mic; see TODO.md), which is what the segmenter would have handed the
server. Embeddings are cached in eval/results/_cache_spk.jsonl so re-runs with
different thresholds are instant.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from eval.ser_eval import RAVDESS_DIR, parse_name, read_wav_16k
from server import config, speaker

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"
CACHE = RESULTS / "_cache_spk.jsonl"
CSV = RESULTS / "spk.csv"

TRIM_RMS = 150.0   # RAVDESS-appropriate VAD level (TODO.md)
GAP_S = 0.2        # response gap inside a glued utterance: the cross-language mode


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def trim(audio: np.ndarray, rms: float = TRIM_RMS) -> np.ndarray:
    """Strip leading/trailing frames below the threshold, like the segmenter would."""
    frame = int(config.SAMPLE_RATE * config.FRAME_MS / 1000)
    n = audio.size // frame
    if n == 0:
        return audio
    f = audio[: n * frame].reshape(n, frame)
    voiced = np.where(np.sqrt((f * f).mean(axis=1)) >= rms / 32768.0)[0]
    if voiced.size == 0:
        return audio
    return audio[voiced[0] * frame: (voiced[-1] + 1) * frame]


def load_index() -> list[dict]:
    if not RAVDESS_DIR.exists():
        raise SystemExit(f"RAVDESS not found at {RAVDESS_DIR} (see eval/ser_eval.py)")
    items = []
    for p in sorted(RAVDESS_DIR.rglob("03-01-*.wav")):
        meta = parse_name(p)
        if meta:
            meta["file"] = str(p.relative_to(ROOT))
            items.append(meta)
    return items


def _load_cache() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if CACHE.exists():
        for line in CACHE.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                out[row["file"]] = row
            except ValueError:
                continue
    return out


def embed_all(items: list[dict]) -> dict[str, dict]:
    """Whole-clip embedding, window embeddings and trimmed audio length per clip."""
    cache = _load_cache()
    todo = [it for it in items if it["file"] not in cache]
    if todo:
        if not speaker.available():
            raise SystemExit("speaker model unavailable; check the startup warning")
        print(f"embedding {len(todo)} clips (cached: {len(cache)})...", flush=True)
        RESULTS.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        with CACHE.open("a", encoding="utf-8") as fh:
            for i, it in enumerate(todo, 1):
                audio = trim(read_wav_16k(ROOT / it["file"]))
                whole = speaker.embed(audio)
                wins = speaker.embed_windows(audio, speech_rms=TRIM_RMS)
                row = {
                    "file": it["file"],
                    "dur_s": round(audio.size / config.SAMPLE_RATE, 3),
                    "whole": None if whole is None else whole.round(5).tolist(),
                    "windows": [w.round(5).tolist() for w in wins],
                }
                fh.write(json.dumps(row) + "\n")
                cache[it["file"]] = row
                if i % 100 == 0 or i == len(todo):
                    print(f"  {i}/{len(todo)}  {time.perf_counter() - t0:.0f}s", flush=True)
    for it in items:
        row = cache[it["file"]]
        it["dur_s"] = row["dur_s"]
        it["whole"] = None if row["whole"] is None else np.asarray(row["whole"], np.float32)
        it["windows"] = [np.asarray(w, np.float32) for w in row["windows"]]
    return cache


def by_actor(items: list[dict]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = defaultdict(list)
    for it in items:
        if it["whole"] is not None:
            out[it["actor"]].append(it)
    return out


def centroid(embs: list[np.ndarray]) -> np.ndarray:
    return speaker._unit(np.mean(np.stack(embs), axis=0))


def enrol_calm(clips: list[dict], n: int = 6) -> list[dict]:
    calm = [c for c in clips if c["ravdess_emotion"] in ("calm", "neutral")]
    return calm[:n]


def enrol_spread(clips: list[dict]) -> list[dict]:
    """One clip per RAVDESS emotion -- the enrolment the guided page aims for."""
    seen: dict[str, dict] = {}
    for c in clips:
        seen.setdefault(c["ravdess_emotion"], c)
    return list(seen.values())


# ---------------------------------------------------------------------------
# --eer
# ---------------------------------------------------------------------------

def eer(scores_t: list[float], scores_n: list[float]) -> float:
    t = np.sort(np.asarray(scores_t))
    n = np.sort(np.asarray(scores_n))
    best = 1.0
    for thr in np.linspace(-1, 1, 2001):
        frr = float(np.mean(t < thr))
        far = float(np.mean(n >= thr))
        if abs(frr - far) < best:
            best, val = abs(frr - far), (frr + far) / 2
    return val


def run_eer(actors: dict[int, list[dict]], rng: random.Random) -> None:
    print("\n== verification EER (one clip vs one enrolled profile) ==")
    for name, pick in (("calm/neutral x6", enrol_calm), ("all 8 emotions", enrol_spread)):
        targets, nontargets = [], []
        per_emotion: dict[str, list[float]] = defaultdict(list)
        for actor, clips in actors.items():
            enrol = pick(clips)
            enrol_files = {c["file"] for c in enrol}
            c = centroid([e["whole"] for e in enrol])
            for t in clips:
                if t["file"] in enrol_files:
                    continue
                s = float(c @ t["whole"])
                targets.append(s)
                per_emotion[t["ravdess_emotion"]].append(s)
            others = [t for a, cl in actors.items() if a != actor for t in cl]
            for t in rng.sample(others, min(60, len(others))):
                nontargets.append(float(c @ t["whole"]))
        print(f"  enrol {name:16s}  EER {100 * eer(targets, nontargets):5.1f}%   "
              f"target mean {np.mean(targets):.3f}  non-target mean {np.mean(nontargets):.3f}")
        if pick is enrol_calm:
            print("    same-speaker cosine by test emotion (calm enrolment):")
            print("    " + " | ".join(f"{k} {np.mean(v):.3f}" for k, v in
                                      sorted(per_emotion.items(), key=lambda kv: -np.mean(kv[1]))))


# ---------------------------------------------------------------------------
# --pairs
# ---------------------------------------------------------------------------

def run_pairs(actors: dict[int, list[dict]], n_pairs: int, rng: random.Random,
              writer: csv.DictWriter | None) -> None:
    print(f"\n== nearest centroid on {n_pairs} random actor pairs (user A, partner B) ==")
    ids = sorted(actors)
    for name, pick in (("calm", enrol_calm), ("spread", enrol_spread)):
        for rule in ("threshold", "online partner"):
            tot = ok = u_tot = u_ok = p_tot = p_ok = 0
            same_sex = [0, 0]
            opp_sex = [0, 0]
            for k in range(n_pairs):
                a, b = rng.sample(ids, 2)
                enrol = pick(actors[a])
                enrol_files = {c["file"] for c in enrol}
                user_c = centroid([e["whole"] for e in enrol])
                turns = [(t, "user") for t in actors[a] if t["file"] not in enrol_files]
                turns = rng.sample(turns, min(20, len(turns)))
                turns += [(t, "other") for t in rng.sample(actors[b], 20)]
                rng.shuffle(turns)
                session = speaker.Session()
                for t, truth in turns:
                    partner = session.partner if rule == "online partner" else None
                    label, frac, _ = speaker.classify_windows([t["whole"]], user_c, partner)
                    pred = "user" if label == "user" else "other"
                    if rule == "online partner" and pred == "other":
                        session.update_partner([t["whole"]])
                    hit = pred == truth
                    tot += 1; ok += hit
                    if truth == "user":
                        u_tot += 1; u_ok += hit
                    else:
                        p_tot += 1; p_ok += hit
                    bucket = same_sex if (a % 2) == (b % 2) else opp_sex
                    bucket[0] += 1; bucket[1] += hit
                    if writer and name == "spread" and rule == "online partner":
                        writer.writerow({"check": "pairs", "pair": k, "user_actor": a,
                                         "partner_actor": b, "file": t["file"],
                                         "truth": truth, "pred": pred,
                                         "score": round(frac, 3), "correct": int(hit)})
            print(f"  {name:6s} {rule:15s}  acc {100 * ok / tot:5.1f}%  user kept "
                  f"{100 * u_ok / u_tot:5.1f}%  partner kept {100 * p_ok / p_tot:5.1f}%  "
                  f"(same-sex {100 * same_sex[1] / max(1, same_sex[0]):.1f}%, "
                  f"opposite {100 * opp_sex[1] / max(1, opp_sex[0]):.1f}%)")


# ---------------------------------------------------------------------------
# --label
# ---------------------------------------------------------------------------

def run_label(actors: dict[int, list[dict]], n_pairs: int, rng: random.Random,
              writer: csv.DictWriter | None) -> None:
    """3-way label on glued utterances, from window fractions. Never cuts audio."""
    print(f"\n== 3-way label on glued turns ({n_pairs} pairs; lo={config.SPEAKER_USER_FRAC_LO}"
          f" hi={config.SPEAKER_USER_FRAC_HI} thr={config.SPEAKER_MATCH_THRESHOLD}) ==")
    if not speaker.available():
        raise SystemExit("speaker model unavailable")
    ids = sorted(actors)
    conf: dict[str, dict[str, int]] = {k: defaultdict(int) for k in ("user", "other", "mixed")}
    fracs: dict[str, list[float]] = defaultdict(list)
    gap = np.zeros(int(GAP_S * config.SAMPLE_RATE), np.float32)
    t0 = time.perf_counter()
    for k in range(n_pairs):
        a, b = rng.sample(ids, 2)
        enrol = enrol_spread(actors[a])
        enrol_files = {c["file"] for c in enrol}
        user_c = centroid([e["whole"] for e in enrol])
        session = speaker.Session()
        pool_a = [t for t in actors[a] if t["file"] not in enrol_files]
        # Warm the partner model the way a conversation would: a few partner
        # turns first, scored from the cache.
        for t in rng.sample(actors[b], 3):
            lab, _, _ = speaker.classify_windows(t["windows"], user_c, session.partner)
            if lab == "other":
                session.update_partner(t["windows"])
        cases = []
        for _ in range(3):
            ta, tb = rng.choice(pool_a), rng.choice(actors[b])
            cases.append(("user", ta["windows"], ta["file"]))
            cases.append(("other", tb["windows"], tb["file"]))
            xa = trim(read_wav_16k(ROOT / ta["file"]))
            xb = trim(read_wav_16k(ROOT / tb["file"]))
            glued = np.concatenate([xa, gap, xb]) if rng.random() < 0.5 else \
                np.concatenate([xb, gap, xa])
            cases.append(("mixed", speaker.embed_windows(glued, speech_rms=TRIM_RMS),
                          f"{ta['file']}+{tb['file']}"))
        rng.shuffle(cases)
        for truth, wins, fname in cases:
            label, frac, _ = speaker.classify_windows(wins, user_c, session.partner)
            if label == "other":
                session.update_partner(wins)
            conf[truth][label] += 1
            fracs[truth].append(frac)
            if writer:
                writer.writerow({"check": "label", "pair": k, "user_actor": a,
                                 "partner_actor": b, "file": fname, "truth": truth,
                                 "pred": label, "score": round(frac, 3),
                                 "correct": int(label == truth)})
        if (k + 1) % 20 == 0:
            print(f"  {k + 1}/{n_pairs}  {time.perf_counter() - t0:.0f}s", flush=True)

    print("  user-window fraction:  " + "  ".join(
        f"{k} {np.mean(v):.2f}" for k, v in fracs.items()))
    print("  truth \\ pred   user   other   mixed")
    total = correct = 0
    for truth in ("user", "other", "mixed"):
        row = conf[truth]
        n = sum(row.values()) or 1
        total += n; correct += row[truth]
        print(f"  {truth:12s}" + "".join(f"{100 * row[p] / n:7.0f}%" for p in ("user", "other", "mixed")))
    dropped = conf["other"]["user"] / max(1, sum(conf["other"].values()))
    print(f"  overall {100 * correct / total:.1f}%   partner labelled as user (silently "
          f"dropped): {100 * dropped:.1f}%")


# ---------------------------------------------------------------------------
# --real
# ---------------------------------------------------------------------------

def run_real(user_wav: Path, conv_wav: Path, partner_wav: Path | None) -> None:
    """Enrol from a recording of the user, then label every utterance of a conversation."""
    from server.segmenter import Segmenter

    def utterances(path: Path) -> list[np.ndarray]:
        audio = read_wav_16k(path)
        seg = Segmenter()
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
        out = seg.feed(pcm)
        tail = seg.flush()
        if tail is not None:
            out.append(tail)
        return out

    prof = speaker.Profile(path=str(RESULTS / "_real_profile.json"))
    prof.clear()
    prof.add([e for e in (speaker.embed(u) for u in utterances(user_wav)) if e is not None])
    print(f"enrolled {prof.count} utterances from {user_wav}")
    session = speaker.Session()
    if partner_wav:
        for u in utterances(partner_wav):
            session.update_partner(speaker.embed_windows(u))
        print(f"partner primed from {partner_wav}")
    t = 0.0
    for u in utterances(conv_wav):
        wins = speaker.embed_windows(u)
        label, frac, cos = speaker.classify_windows(wins, prof.centroid, session.partner)
        if label == "other":
            session.update_partner(wins)
        dur = u.size / config.SAMPLE_RATE
        print(f"  {t:6.1f}s  {dur:4.1f}s  {label:7s} frac {frac:.2f} cos {cos:.2f} win {len(wins)}")
        t += dur
    prof.clear()


# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eer", action="store_true")
    ap.add_argument("--pairs", type=int, nargs="?", const=120, default=None)
    ap.add_argument("--label", action="store_true")
    ap.add_argument("--label-pairs", type=int, default=60)
    ap.add_argument("--real", nargs=2, metavar=("USER_WAV", "CONV_WAV"))
    ap.add_argument("--partner", type=Path, default=None, help="with --real: a partner-only WAV")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--csv", type=Path, default=CSV)
    args = ap.parse_args()

    if args.real:
        run_real(Path(args.real[0]), Path(args.real[1]), args.partner)
        return

    everything = not (args.eer or args.pairs is not None or args.label)
    rng = random.Random(args.seed)
    items = load_index()
    embed_all(items)
    actors = by_actor(items)
    print(f"{len(items)} clips, {len(actors)} actors, mean trimmed length "
          f"{np.mean([it['dur_s'] for it in items]):.2f}s, model {config.SPEAKER_MODEL}")

    RESULTS.mkdir(parents=True, exist_ok=True)
    with args.csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["check", "pair", "user_actor", "partner_actor",
                                                "file", "truth", "pred", "score", "correct"])
        writer.writeheader()
        if everything or args.eer:
            run_eer(actors, rng)
        if everything or args.pairs is not None:
            run_pairs(actors, args.pairs or 120, rng, writer)
        if everything or args.label:
            run_label(actors, args.label_pairs, rng, writer)
    print(f"\nrows -> {args.csv}")


if __name__ == "__main__":
    main()

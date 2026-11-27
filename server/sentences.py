"""Split one transcribed utterance into sentence-sized pieces. Pure logic, zero I/O.

Why this exists
---------------
The segmenter closes an utterance on END_SILENCE_MS of quiet or at the
MAX_UTTERANCE_MS cap. In real rooms the quiet often never registers: of the
first 256 logged utterances (2026-09), 140 ran into the 15 s cap, the median was
25 words, and 48% held three or more sentences. Parakeet's own token timings
showed 1.8 s and 3.5 s pauses inside one of them that the energy VAD counted
as speech. A read of a 15 s paragraph is too vague to help the listener, and
its SER label is an average over every sentence in it.

So after ASR, cut the utterance at sentence ends, using the per-token
timestamps the transducer already returns, and treat each piece as its own
utterance: its own SER, its own read, the earlier pieces as context. This is
deliberately NOT a VAD change. It works however the room fools the VAD, and it
never cuts inside a sentence, which is the failure the speaker-split work
measured and rejected (docs/status/TODO.md): half a sentence is a worse input
than a long one.

Rules, in order:
  1. A sentence ends at a token ending in `.`, `?` or `!` (ASR punctuation).
  2. Sentences shorter than `min_s` seconds or `min_words` words are merged
     into the next one (the last into the previous), because SER is unreliable
     on very short audio (confidence.py penalises under ~1.5 s voiced) and
     "Oh." alone gives the interpreter nothing to read.
  3. Over `max_pieces`, the shortest adjacent pair is merged until it fits.
  4. Audio for a piece runs from `pad_s` before its first token to `pad_s` after
     its last, clamped to the midpoint of the gap to its neighbours, so pieces
     never overlap and a long pause is not handed to SER as part of a sentence.

One piece means "do not split": the caller then runs today's path unchanged.
Token timestamps are token START times (sherpa-onnx transducer output); a
token's end is approximated as start + 0.2 s.
"""

from __future__ import annotations

from dataclasses import dataclass

_ENDS = (".", "?", "!")


@dataclass(frozen=True)
class Token:
    text: str            # as the decoder emits it, leading space marks a word start
    t: float             # start time in seconds from the start of the utterance
    logprob: float | None = None


@dataclass(frozen=True)
class Piece:
    text: str
    start: int           # sample index, inclusive
    end: int             # sample index, exclusive
    first: int           # token index range [first, last]
    last: int

    def asr_conf(self, tokens: list[Token]) -> float | None:
        """Same statistic as transcriber._Sherpa: exp(min token log-prob)."""
        import math

        lps = [tk.logprob for tk in tokens[self.first:self.last + 1] if tk.logprob is not None]
        return round(min(1.0, math.exp(min(lps))), 3) if lps else None


def _words(tokens: list[Token], a: int, b: int) -> int:
    text = "".join(tk.text for tk in tokens[a:b + 1])
    return sum(1 for w in text.split() if any(c.isalnum() for c in w))


def split(
    tokens: list[Token],
    n_samples: int,
    sample_rate: int,
    *,
    min_s: float = 1.5,
    min_words: int = 3,
    max_pieces: int = 6,
    pad_s: float = 0.25,
) -> list[Piece]:
    """Pieces in time order; a single piece means the utterance stays whole."""
    if not tokens or n_samples <= 0:
        return []
    total_s = n_samples / sample_rate

    # 1. sentence spans as inclusive token-index ranges
    spans: list[list[int]] = []
    a = 0
    for i, tk in enumerate(tokens):
        if tk.text.strip().endswith(_ENDS) and i < len(tokens) - 1:
            # Punctuation glued to the next token ("." then "." of "...") stays
            # with this sentence; a sentence needs a following word to end.
            nxt = tokens[i + 1].text
            if nxt.strip() and nxt.strip()[0] not in _ENDS:
                spans.append([a, i])
                a = i + 1
    spans.append([a, len(tokens) - 1])

    def dur(s: list[int]) -> float:
        # Spoken extent: first token start to the last token's start, plus a
        # nominal 0.2 s for the last token itself. Using t_end here would count
        # the pause after the sentence as part of it.
        return tokens[s[1]].t - tokens[s[0]].t + 0.2

    def small(s: list[int]) -> bool:
        return dur(s) < min_s or _words(tokens, s[0], s[1]) < min_words

    # 2. merge short sentences forward (the last one backward)
    merged: list[list[int]] = []
    carry: list[int] | None = None
    for s in spans:
        if carry is not None:
            s = [carry[0], s[1]]
            carry = None
        if small(s):
            carry = s
        else:
            merged.append(s)
    if carry is not None:
        if merged:
            merged[-1] = [merged[-1][0], carry[1]]
        else:
            merged.append(carry)

    # 3. cap the count by merging the shortest adjacent pair
    while len(merged) > max(1, max_pieces):
        k = min(range(len(merged) - 1), key=lambda j: dur([merged[j][0], merged[j + 1][1]]))
        merged[k:k + 2] = [[merged[k][0], merged[k + 1][1]]]

    # 4. audio bounds: padded, clamped to the gap midpoints, never overlapping
    pieces: list[Piece] = []
    for j, (f, l) in enumerate(merged):
        lo = max(0.0, tokens[f].t - pad_s)
        hi = min(total_s, tokens[l].t + 0.2 + pad_s)
        if j > 0:
            prev_last = merged[j - 1][1]
            mid = (tokens[prev_last].t + tokens[f].t) / 2
            lo = max(lo, mid)
        if j < len(merged) - 1:
            nxt_first = merged[j + 1][0]
            mid = (tokens[l].t + tokens[nxt_first].t) / 2
            hi = min(hi, mid)
        if j == 0:
            lo = 0.0                     # keep the segmenter's pre-roll on piece one
        if j == len(merged) - 1:
            hi = total_s                 # and the tail on the last
        text = "".join(tk.text for tk in tokens[f:l + 1]).strip()
        pieces.append(Piece(text=text,
                            start=int(round(lo * sample_rate)),
                            end=max(int(round(hi * sample_rate)), int(round(lo * sample_rate)) + 1),
                            first=f, last=l))
    return pieces


def tokens_from_text(text: str, seconds_per_word: float = 0.4) -> list[Token]:
    """Test helper: one Token per word at a steady pace. Not used by the server."""
    out: list[Token] = []
    t = 0.2
    for w in text.split():
        out.append(Token(" " + w, t))
        t += seconds_per_word
    return out


if __name__ == "__main__":
    SR = 16_000

    # 1. one sentence -> one piece, the whole audio
    tk = tokens_from_text("I really think we should go now.")
    p = split(tk, 5 * SR, SR)
    assert len(p) == 1 and p[0].start == 0 and p[0].end == 5 * SR, p

    # 2. three real sentences -> three pieces, in order, non-overlapping, covering all
    tk = tokens_from_text("I am so happy you came today. "
                          "The traffic on the way was terrible though. "
                          "Anyway let us get some food now.")
    n = int((tk[-1].t + 1.0) * SR)
    p = split(tk, n, SR)
    assert [x.text for x in p] == ["I am so happy you came today.",
                                    "The traffic on the way was terrible though.",
                                    "Anyway let us get some food now."], p
    assert p[0].start == 0 and p[-1].end == n
    assert all(a.end <= b.start for a, b in zip(p, p[1:])), p

    # 3. short sentences merge forward: "Oh." "Wow." join the next sentence
    tk = tokens_from_text("Oh. Wow. That is the best news I have heard all week. "
                          "We should celebrate with everyone tonight.")
    p = split(tk, int((tk[-1].t + 1) * SR), SR)
    assert p[0].text.startswith("Oh. Wow. That is"), p
    assert len(p) == 2, p

    # 4. a short final sentence merges backward
    tk = tokens_from_text("We spent the whole afternoon fixing the car together. Fine.")
    p = split(tk, int((tk[-1].t + 1) * SR), SR)
    assert len(p) == 1 and p[0].text.endswith("Fine."), p

    # 5. a long pause between sentences is split at its midpoint, not padded into SER
    tk = [Token(" Nice", 1.0), Token(" and", 1.2), Token(" strong", 1.4), Token(" today", 1.6),
          Token(".", 1.8), Token(" Shaking", 6.0), Token(" and", 6.2), Token(" begging", 6.4),
          Token(" again", 6.6), Token(".", 6.8)]
    p = split(tk, 8 * SR, SR, min_s=0.5)
    assert len(p) == 2, p
    assert p[0].end <= int(2.3 * SR), p[0]          # 1.8 + 0.2 + pad, not out to 3.9 s
    assert p[1].start >= int(5.7 * SR), p[1]        # 6.0 - pad
    assert p[1].end == 8 * SR

    # 6. max_pieces caps the count by merging neighbours
    tk = tokens_from_text(" ".join(["This is sentence number %d here." % i for i in range(10)]))
    p = split(tk, int((tk[-1].t + 1) * SR), SR, max_pieces=4)
    assert len(p) == 4 and all(a.end <= b.start for a, b in zip(p, p[1:])), p

    # 7. no tokens -> nothing (the caller keeps the utterance whole)
    assert split([], SR, SR) == []

    # 8. "..." does not end a sentence mid-ellipsis
    tk = [Token(" Well", 0.2), Token(".", 0.4), Token(".", 0.45), Token(".", 0.5),
          Token(" I", 1.0), Token(" guess", 1.2), Token(" so", 1.4), Token(" then", 1.6)]
    p = split(tk, 3 * SR, SR, min_s=0.1, min_words=1)
    assert [x.text for x in p] == ["Well...", "I guess so then"], p

    # 9. per-piece ASR confidence is the min over that piece's tokens
    tk = [Token(" Hello", 0.2, -0.1), Token(" there", 0.5, -2.0), Token(".", 0.7, -0.1)]
    assert split(tk, SR, SR)[0].asr_conf(tk) == round(2.718281828 ** -2.0, 3)

    print("sentences self-test: 9/9 passed")

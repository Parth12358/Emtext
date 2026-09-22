"""Energy-based utterance segmenter (pure logic, no I/O).

This module turns a *stream* of raw PCM bytes into discrete *utterances*: the
chunks of audio that (probably) contain one spoken sentence. It does this with
a small state machine driven by the short-term energy (RMS) of 30 ms frames.

Why energy-based and not a neural VAD here?
  - It is cheap, deterministic, and has zero model dependencies, so it can run
    on the audio hot path without ever blocking.
  - faster-whisper does its own (better) VAD downstream; this stage only needs
    to be good enough to slice the stream into roughly sentence-sized pieces so
    we can transcribe + interpret them as they land.

Everything here is deliberately free of file/network/async concerns so it can
be unit-tested with synthetic audio (see the __main__ block at the bottom).
"""

from __future__ import annotations

from collections import deque

import numpy as np

from . import config


class Segmenter:
    """Streaming state machine: feed PCM bytes in, get finished utterances out.

    The machine has two states:

        IDLE   -- we are waiting for speech. We keep a short ring buffer of the
                  most recent frames so that when speech *does* start we can
                  prepend the PRE_ROLL that leads into it (otherwise the first
                  consonant is always clipped, which wrecks transcription).
                  While idle we also listen to the *room*: every frame that is
                  not speech updates a running noise floor, and the speech
                  threshold is a margin above that floor (see `_threshold`).

        SPEECH -- we are inside an utterance, appending every frame. We watch a
                  trailing-silence timer: once it grows past END_SILENCE_MS the
                  talker has stopped, so we close the utterance. A separate hard
                  cap (MAX_UTTERANCE_MS) force-cuts run-on audio so a noisy room
                  can never make us buffer forever.

    Frames are a fixed FRAME_MS long. Working in whole frames (rather than per
    sample) keeps all the timers as simple integer multiples of FRAME_MS.
    """

    def __init__(
        self,
        sample_rate: int = config.SAMPLE_RATE,
        frame_ms: int = config.FRAME_MS,
        speech_rms: float = config.SPEECH_RMS,
        end_silence_ms: int = config.END_SILENCE_MS,
        min_utterance_ms: int = config.MIN_UTTERANCE_MS,
        max_utterance_ms: int = config.MAX_UTTERANCE_MS,
        pre_roll_ms: int = config.PRE_ROLL_MS,
        adaptive: bool = config.VAD_ADAPTIVE,
        speech_rms_min: float = config.SPEECH_RMS_MIN,
        noise_margin: float = config.NOISE_MARGIN,
        noise_floor_rise: float = config.NOISE_FLOOR_RISE,
        noise_floor_fall: float = config.NOISE_FLOOR_FALL,
        noise_floor_init: float | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        # `speech_rms_max` is the configured SPEECH_RMS: the ceiling of the
        # adaptive threshold, and the whole threshold when adaptive is off.
        # `speech_rms` is the EFFECTIVE threshold for the most recent frame --
        # kept as a plain attribute (not a property) so main.py's telemetry and
        # the eval scripts keep reading it exactly as they did before.
        self.speech_rms_max = speech_rms
        self.speech_rms = speech_rms
        self.adaptive = adaptive
        self.speech_rms_min = min(speech_rms_min, speech_rms)
        self.noise_margin = noise_margin
        self.noise_floor_rise = noise_floor_rise
        self.noise_floor_fall = noise_floor_fall
        # Start the floor where the OLD constant threshold would put it, so the
        # first few hundred milliseconds behave like before rather than like a
        # silent room (a live mic is never silent, and assuming so would open an
        # utterance on the first breath).
        if noise_floor_init is None:
            noise_floor_init = config.NOISE_FLOOR_INIT if adaptive else 0.0
        self.noise_floor: float = float(noise_floor_init)
        self.end_silence_ms = end_silence_ms
        self.min_utterance_ms = min_utterance_ms
        self.max_utterance_ms = max_utterance_ms

        # Samples per analysis frame, e.g. 16000 * 30/1000 = 480 samples.
        self.frame_samples = int(sample_rate * frame_ms / 1000)
        # Each int16 sample is 2 bytes; a frame is this many bytes on the wire.
        self.frame_bytes = self.frame_samples * 2

        # How many whole frames make up the pre-roll ring buffer. We round down;
        # a little less pre-roll is harmless, over-allocating is not.
        self.pre_roll_frames = max(0, pre_roll_ms // frame_ms)

        # --- mutable state ---------------------------------------------------
        # Leftover bytes that did not fill a whole frame on the previous feed().
        # A caller may hand us any number of bytes, so we must stitch partial
        # frames across calls to stay aligned to sample/frame boundaries.
        self._byte_buffer = bytearray()

        # Ring buffer of recent frames while IDLE (int16 arrays), for pre-roll.
        self._pre_roll: deque[np.ndarray] = deque(maxlen=self.pre_roll_frames)

        # --- observable state (read by main.py for VAD telemetry) -----------
        # Public and plainly named: these are diagnostics, not internals. They
        # are written but never read by the state machine, so they cannot change
        # segmentation behaviour.
        self.last_rms: float = 0.0
        self.peak_rms: float = 0.0
        self.frames_seen: int = 0
        self.voiced_frames_seen: int = 0
        self.discarded: int = 0
        self.last_close_reason: str | None = None
        self.last_voiced_ms: int = 0
        self.last_utterance_ms: int = 0

        self._in_speech = False
        self._utterance_frames: list[np.ndarray] = []  # int16 frames of current utterance
        self._voiced_ms = 0            # how much of the utterance was above threshold
        self._trailing_silence_ms = 0  # consecutive quiet at the tail, drives the end timer

    # -- public API ----------------------------------------------------------

    def feed(self, pcm_bytes: bytes) -> list[np.ndarray]:
        """Consume raw PCM bytes; return any utterances that completed.

        Returns a list (possibly empty) of float32 arrays in [-1, 1] -- the
        format faster-whisper wants. Most feeds return [] and the occasional
        feed returns one (rarely more) finished utterance.
        """
        self._byte_buffer.extend(pcm_bytes)
        completed: list[np.ndarray] = []

        # Peel off as many whole frames as we now have; keep the remainder.
        while len(self._byte_buffer) >= self.frame_bytes:
            raw = bytes(self._byte_buffer[: self.frame_bytes])
            del self._byte_buffer[: self.frame_bytes]
            frame = np.frombuffer(raw, dtype=np.int16)
            done = self._process_frame(frame)
            if done is not None:
                completed.append(done)

        return completed

    def flush(self) -> np.ndarray | None:
        """Close out any in-progress utterance (e.g. on disconnect).

        Returns the utterance if it clears the MIN_UTTERANCE_MS bar, else None.
        """
        if self._in_speech:
            return self._finalize()
        return None

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _rms(frame: np.ndarray) -> float:
        """Root-mean-square energy of an int16 frame.

        We compute in float64 to avoid int16 overflow when squaring (a full
        scale sample squared is ~1.07e9, well past int16's range).
        """
        if frame.size == 0:
            return 0.0
        f = frame.astype(np.float64)
        return float(np.sqrt(np.mean(f * f)))

    def _process_frame(self, frame: np.ndarray) -> np.ndarray | None:
        """Advance the state machine by one frame; return an utterance if one
        just finished, otherwise None."""
        rms = self._rms(frame)
        is_speech = rms >= self._threshold()

        # --- noise floor ----------------------------------------------------
        # Learn the room from frames that are NOT speech, and only those: a
        # sentence must never lift the floor under itself, or a long loud line
        # would raise the bar mid-utterance and cut its own tail. The floor is
        # an exponential moving average with two rates. It FALLS fast (a quiet
        # room should be trusted within a few frames, and that is what lets
        # quiet speech through) and RISES slowly (a door slam or a cough is a
        # transient, not a new room, and the threshold should not jump for it).
        # The decision above used the floor from the previous frame on purpose:
        # this frame's classification must not depend on itself.
        if self.adaptive and not is_speech:
            alpha = self.noise_floor_fall if rms < self.noise_floor else self.noise_floor_rise
            self.noise_floor += alpha * (rms - self.noise_floor)
        self.speech_rms = self._threshold()

        # --- observability -------------------------------------------------
        # Recorded, never acted on: the state machine below is unchanged. This
        # exists because the VAD is the one stage you cannot debug by looking at
        # its output -- a split sentence and a clipped word look identical in the
        # transcript, but have opposite fixes. main.py reads these to build the
        # `vad` telemetry frame; nothing here does I/O, so the module stays pure
        # and the __main__ self-test is unaffected.
        self.last_rms = float(rms)
        self.frames_seen += 1
        if is_speech:
            self.voiced_frames_seen += 1
        # Peak since the last utterance closed: the single most useful number
        # for deciding whether SPEECH_RMS is set sanely for a given microphone.
        self.peak_rms = max(self.peak_rms, float(rms))

        if not self._in_speech:
            # IDLE: remember this frame for pre-roll, and watch for onset.
            if is_speech:
                # Speech onset. Seed the utterance with the pre-roll frames that
                # led into it, then this frame. Pre-roll frames are pre-speech,
                # so they do NOT count toward voiced_ms.
                self._utterance_frames = list(self._pre_roll)
                self._utterance_frames.append(frame)
                self._voiced_ms = self.frame_ms
                self._trailing_silence_ms = 0
                self._in_speech = True
                self._pre_roll.clear()
            else:
                self._pre_roll.append(frame)
            return None

        # SPEECH: keep every frame (silence in the middle is part of the sentence).
        self._utterance_frames.append(frame)
        if is_speech:
            self._voiced_ms += self.frame_ms
            self._trailing_silence_ms = 0  # reset: the talker is still going
        else:
            self._trailing_silence_ms += self.frame_ms

        # End condition 1: enough trailing silence -> the talker stopped.
        if self._trailing_silence_ms >= self.end_silence_ms:
            self.last_close_reason = "end_silence"
            return self._finalize()

        # End condition 2: hard length cap -> force-cut a run-on utterance so we
        # never buffer without bound. The next frame simply starts fresh in IDLE.
        if len(self._utterance_frames) * self.frame_ms >= self.max_utterance_ms:
            self.last_close_reason = "max_length"
            return self._finalize()

        return None

    def _threshold(self) -> float:
        """The RMS a frame must reach to count as speech, right now.

        With adaptation off this is simply SPEECH_RMS, byte for byte the old
        behaviour. With it on, the threshold is a fixed ratio above the measured
        noise floor, clamped from both sides:

          * the CEILING is SPEECH_RMS (500): a loud room asks for no more than
            the old constant did, so nothing that used to segment stops
            segmenting;
          * the FLOOR is SPEECH_RMS_MIN (150): digital silence -- a studio clip,
            a muted line -- must not drive the threshold to zero and open an
            utterance on the first bit of dither. 150 is where RAVDESS measured
            5/10 dropped clips becoming 1/10 (docs/status/TODO.md).

        Between those, a quiet room with a floor of 30 asks for 150, a normal
        room at 100 asks for 300, and an air-conditioned office at 200 asks for
        the full 500 -- which is the point: one number that means "a margin
        above whatever this microphone hears when nobody is talking".
        """
        if not self.adaptive:
            return self.speech_rms_max
        wanted = self.noise_floor * self.noise_margin
        return float(min(self.speech_rms_max, max(self.speech_rms_min, wanted)))

    def _finalize(self) -> np.ndarray | None:
        """Close the current utterance and reset to IDLE.

        Discards utterances with too little *voiced* audio (coughs, clicks, a
        single loud tap) -- those never had MIN_UTTERANCE_MS of real speech.
        """
        frames = self._utterance_frames
        voiced_ms = self._voiced_ms

        # Reset state before we return so the next frame starts clean.
        self._in_speech = False
        self._utterance_frames = []
        self._voiced_ms = 0
        self._trailing_silence_ms = 0
        self._pre_roll.clear()

        self.last_voiced_ms = voiced_ms
        self.last_utterance_ms = len(frames) * self.frame_ms
        if voiced_ms < self.min_utterance_ms or not frames:
            # Discarded as a blip. This is the failure mode that leaves NO trace
            # anywhere else -- the user spoke and simply got nothing back -- so
            # it is worth counting explicitly.
            self.discarded += 1
            self.last_close_reason = "too_short"
            return None

        # Concatenate int16 frames and scale to float32 in [-1, 1]. Dividing by
        # 32768 (not 32767) keeps the transform exact for the most-negative
        # sample and is the conventional int16->float mapping whisper expects.
        pcm = np.concatenate(frames).astype(np.float32) / 32768.0
        return pcm


# ---------------------------------------------------------------------------
# Self-test: run `python -m server.segmenter` from the project's `emtext/`
# directory. Uses synthetic audio only -- no mic, no files.
# ---------------------------------------------------------------------------
if __name__ == "__main__":

    def _tone(ms: int, freq: float = 220.0, amp: int = 8000) -> bytes:
        """A mono int16 sine tone of `ms` milliseconds (RMS ~amp/sqrt(2))."""
        n = int(config.SAMPLE_RATE * ms / 1000)
        t = np.arange(n) / config.SAMPLE_RATE
        wave = (amp * np.sin(2 * np.pi * freq * t)).astype(np.int16)
        return wave.tobytes()

    def _silence(ms: int) -> bytes:
        n = int(config.SAMPLE_RATE * ms / 1000)
        return np.zeros(n, dtype=np.int16).tobytes()

    # Case 1: silence + a clear 1s tone + trailing silence -> exactly one utterance.
    seg = Segmenter()
    stream = _silence(500) + _tone(1000) + _silence(1000)
    utterances = seg.feed(stream)
    tail = seg.flush()  # nothing should be mid-utterance; the trailing silence closed it
    if tail is not None:
        utterances.append(tail)
    assert len(utterances) == 1, f"expected 1 utterance, got {len(utterances)}"
    # Sanity: the recovered audio should be non-trivial and in range.
    u = utterances[0]
    assert u.dtype == np.float32 and u.max() <= 1.0 and u.min() >= -1.0
    print(f"case 1 OK: one utterance, {u.size} samples "
          f"({u.size / config.SAMPLE_RATE * 1000:.0f} ms)")

    # Case 2: a 200 ms blip is shorter than MIN_UTTERANCE_MS (350) -> discarded.
    seg2 = Segmenter()
    blip = _silence(400) + _tone(200) + _silence(1000)
    got2 = [u for u in seg2.feed(blip) if u is not None]
    assert len(got2) == 0, f"expected blip to be discarded, got {len(got2)}"
    print("case 2 OK: 200 ms blip discarded")

    # Case 3: QUIET speech. A tone at amp 300 has RMS ~212 -- below the old
    # constant threshold of 500 (it was silently dropped, which is the bug in
    # docs/status/TODO.md) but above SPEECH_RMS_MIN. After leading silence the adaptive
    # threshold settles at the floor, so the tone must now segment; with
    # adaptation off it must still be dropped, proving the old path is intact.
    quiet = _silence(500) + _tone(1000, amp=300) + _silence(1000)
    seg3 = Segmenter(adaptive=True)   # explicit: the self-test must not depend on VAD_ADAPTIVE
    got3 = seg3.feed(quiet)
    assert len(got3) == 1, f"adaptive: expected quiet tone to segment, got {len(got3)}"
    assert seg3.speech_rms == seg3.speech_rms_min, seg3.speech_rms
    seg3f = Segmenter(adaptive=False)
    got3f = seg3f.feed(quiet)
    assert len(got3f) == 0, f"fixed: expected quiet tone dropped, got {len(got3f)}"
    print(f"case 3 OK: RMS-212 tone segmented adaptively (threshold "
          f"{seg3.speech_rms:.0f}), dropped at fixed {seg3f.speech_rms:.0f}")

    # Case 4: a NOISY room must not false-trigger, and must still hear a loud
    # tone over the noise. White noise at RMS ~400 wants a threshold of 1200,
    # which clamps to the SPEECH_RMS ceiling -- so the adaptive path can never
    # be MORE trigger-happy than the old constant in a loud room.
    rng = np.random.default_rng(7)

    def _noise(ms: int, rms: float = 400.0) -> np.ndarray:
        n = int(config.SAMPLE_RATE * ms / 1000)
        return (rng.standard_normal(n) * rms).astype(np.int16)

    tone_arr = np.frombuffer(_tone(1000), dtype=np.int16)
    mixed = (tone_arr.astype(np.int32) + _noise(1000).astype(np.int32))
    noisy = np.concatenate([
        _noise(2000),
        mixed.clip(-32768, 32767).astype(np.int16),
        _noise(1500),
    ]).tobytes()
    seg4 = Segmenter(adaptive=True)
    got4 = seg4.feed(noisy)
    assert seg4.speech_rms == seg4.speech_rms_max, seg4.speech_rms
    assert len(got4) == 1, f"noisy room: expected exactly one utterance, got {len(got4)}"
    assert seg4.discarded == 0, f"noisy room: {seg4.discarded} false trigger(s) discarded"
    print(f"case 4 OK: noise floor {seg4.noise_floor:.0f} clamps threshold to "
          f"{seg4.speech_rms:.0f}; one utterance, no false triggers")

    # Case 5: speech must not raise the floor. A loud burst is classified as
    # speech and therefore excluded from the floor estimate; the floor after
    # it must be where the surrounding silence put it.
    seg5 = Segmenter(adaptive=True)
    seg5.feed(_silence(600))
    before = seg5.noise_floor
    seg5.feed(_tone(300, amp=8000) + _silence(1000))
    assert abs(seg5.noise_floor - before) < 1.0, (before, seg5.noise_floor)
    print(f"case 5 OK: loud burst left the noise floor at {seg5.noise_floor:.1f}")

    print("segmenter self-test passed")

"""On-box speech-to-text for voice food logging.

Recordings are transcribed by faster-whisper on the CPU inside this container,
so the audio never leaves the machine. The model (``SPEECH_MODEL``, default
``small.en``, ~480 MB) is downloaded once into ``DATA_DIR/models/whisper`` on
first use and kept loaded; a few seconds of speech then transcribes in well
under a second.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
import os
import re
import threading
from typing import Any

from .config import DATA_DIR, env

log = logging.getLogger("glucopilot.speech")

SPEECH_MODEL = env("SPEECH_MODEL", "small.en")
MODEL_DIR = DATA_DIR / "models" / "whisper"
SAMPLE_RATE = 16000
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 90
# Whisper keeps the style and vocabulary of its prompt, which helps with food
# words and gets punctuation right on short clips. "Chips" is here because
# Whisper wrote "Toe chips" for it, and "hash browns" because it wrote "Cash
# browns". No clock time is in it: a time leaked from the prompt into a
# transcript would be logged as when the meal was eaten.
VOCAB_PROMPT = ("Food log. One bag of yogurt bites. A bag of potato chips. Hash browns and eggs. "
                "Chicken sandwich on gluten-free bread with Swiss cheese.")
# Mishearings already seen, put right in case the prompt doesn't prevent them.
# Only ones that can't be meant as said: nobody logs eating "cash".
_MISHEARD = ((re.compile(r"\bcash(?=\s*browns?\b)", re.IGNORECASE), "hash"),)

_model: Any = None
_load_lock = threading.Lock()
_run_lock = threading.Lock()


class SpeechError(Exception):
    """A problem worth showing the person (bad audio, nothing heard)."""


def _load_model() -> Any:
    global _model
    with _load_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            threads = max(1, min(8, os.cpu_count() or 1))
            log.info("loading speech model %s", SPEECH_MODEL)
            _model = WhisperModel(SPEECH_MODEL, device="cpu", compute_type="int8",
                                  cpu_threads=threads, download_root=str(MODEL_DIR))
    return _model


def decode_audio(data: bytes) -> Any:
    """Any browser recording (WebM/Opus, MP4/AAC, Ogg, WAV) -> 16 kHz mono
    float32 samples. Decoded here rather than by faster-whisper, whose own
    decoder doesn't work with current PyAV releases."""
    import av
    import numpy as np

    chunks = []
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    try:
        with av.open(io.BytesIO(data)) as container:
            for frame in container.decode(audio=0):
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().reshape(-1))
            for out in resampler.resample(None):
                chunks.append(out.to_ndarray().reshape(-1))
    except (av.FFmpegError, IndexError, ValueError) as err:
        raise SpeechError("Couldn't read that recording.") from err
    if not chunks:
        raise SpeechError("That recording was empty.")
    samples = np.concatenate(chunks).astype(np.float32) / 32768.0
    if samples.size > MAX_SECONDS * SAMPLE_RATE:
        raise SpeechError(f"Recordings are limited to {MAX_SECONDS} seconds.")
    return samples


def _transcribe_sync(data: bytes) -> str:
    samples = decode_audio(data)
    model = _load_model()
    with _run_lock:
        segments, _info = model.transcribe(samples, language="en", beam_size=5, vad_filter=True,
                                           initial_prompt=VOCAB_PROMPT, condition_on_previous_text=False)
        return put_right(" ".join(s.text.strip() for s in segments).strip())


def put_right(text: str) -> str:
    """The transcript with known mishearings replaced, keeping a capital."""
    for pattern, meant in _MISHEARD:
        text = pattern.sub(lambda m, meant=meant: meant.capitalize() if m.group(0)[0].isupper() else meant, text)
    return text


def _split_payload(audio: str) -> bytes:
    """audio is "media/type|base64", the same encoding the label photo uses."""
    if not audio or "|" not in audio:
        raise SpeechError("A recording is required.")
    try:
        data = base64.b64decode(audio.split("|", 1)[1], validate=True)
    except (binascii.Error, ValueError) as err:
        raise SpeechError("Couldn't read that recording.") from err
    if not data:
        raise SpeechError("That recording was empty.")
    if len(data) > MAX_AUDIO_BYTES:
        raise SpeechError("That recording is too large.")
    return data


async def transcribe(audio: str) -> str:
    data = _split_payload(audio)
    return await asyncio.to_thread(_transcribe_sync, data)

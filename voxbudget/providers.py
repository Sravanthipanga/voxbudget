"""Provider layer — one interface per stage, hot-swappable implementations.

Booming 2025-26 real-time stack this is modelled on:

- **STT:** Deepgram Nova-3 streaming, OpenAI gpt-4o-transcribe / Whisper
- **LLM:** OpenAI Realtime (gpt-realtime), GPT-4o-mini / Claude Haiku class
  fast tiers for the SHORT_REPLIES degradation level
- **TTS:** Cartesia Sonic (40 ms-class first chunk), ElevenLabs Flash v2.5
- **Transport/orchestration:** LiveKit Agents and Pipecat frame pipelines

The default providers are *simulated*: deterministic, latency-shaped stand-ins
that need no API keys, so the pipeline, budget, and degradation behaviour can
be run and tested anywhere. Real adapters are thin classes at the bottom of
this file — they read keys from the environment and are only constructed when
explicitly selected; importing this module never requires a key.
"""

from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ChaosConfig:
    """Fault injection: add delay or failures per stage so timeouts and
    degradation can be demonstrated on demand, not just in production."""

    extra_delay_ms: dict[str, float] = field(default_factory=dict)
    fail_stages: set[str] = field(default_factory=set)

    async def maybe_delay(self, stage: str, rng: random.Random) -> None:
        delay = self.extra_delay_ms.get(stage, 0.0)
        if delay > 0:
            await asyncio.sleep(delay / 1000.0)

    def check_fail(self, stage: str) -> None:
        if stage in self.fail_stages:
            raise RuntimeError(f"chaos: injected failure in stage '{stage}'")


# --------------------------------------------------------------------------
# Protocols
# --------------------------------------------------------------------------
class STTProvider(Protocol):
    name: str

    async def transcribe(self, audio: bytes, *, transcript_hint: str) -> str: ...


class LLMProvider(Protocol):
    name: str

    async def first_token(self, prompt: str, *, max_sentences: int | None) -> str: ...

    async def complete(self, prompt: str, *, max_sentences: int | None) -> str: ...


class TTSProvider(Protocol):
    name: str

    async def first_chunk(self, text: str) -> bytes: ...

    async def synthesise(self, text: str) -> bytes: ...


# --------------------------------------------------------------------------
# Simulated providers — latency-shaped, deterministic, zero keys
# --------------------------------------------------------------------------
@dataclass
class SimulatedSTT:
    name: str = "simulated-stt (Nova-3-shaped)"
    base_ms: float = 120.0
    chaos: ChaosConfig = field(default_factory=ChaosConfig)
    _rng: random.Random = field(default_factory=lambda: random.Random(7))

    async def transcribe(self, audio: bytes, *, transcript_hint: str) -> str:
        self.chaos.check_fail("stt")
        await self.chaos.maybe_delay("stt", self._rng)
        # Streaming STT latency grows weakly with utterance length.
        jitter = self._rng.uniform(0.85, 1.2)
        await asyncio.sleep((self.base_ms * jitter) / 1000.0)
        return transcript_hint.strip()


@dataclass
class SimulatedLLM:
    name: str = "simulated-llm (gpt-realtime-shaped)"
    ttft_ms: float = 160.0
    complete_ms: float = 420.0
    chaos: ChaosConfig = field(default_factory=ChaosConfig)
    _rng: random.Random = field(default_factory=lambda: random.Random(11))

    _ANSWERS = (
        "You asked about {q}. Here's the short version: VoxBudget measured every "
        "stage of this turn against an 800 millisecond budget, and the waterfall "
        "on your screen shows exactly where the time went.",
        "On {q} — the pipeline streamed your speech to text, generated this reply "
        "token by token, and started speaking before the full answer existed. "
        "That overlap is what keeps the conversation feeling real-time.",
    )

    def _reply(self, prompt: str, *, max_sentences: int | None) -> str:
        text = self._ANSWERS[abs(hash(prompt)) % len(self._ANSWERS)].format(
            q=prompt.rstrip("?.!").lower() or "that"
        )
        if max_sentences is not None:
            sentences = text.split(". ")
            text = ". ".join(sentences[:max_sentences]).rstrip(".") + "."
        return text

    async def first_token(self, prompt: str, *, max_sentences: int | None = None) -> str:
        self.chaos.check_fail("llm_first_token")
        await self.chaos.maybe_delay("llm_first_token", self._rng)
        await asyncio.sleep((self.ttft_ms * self._rng.uniform(0.85, 1.25)) / 1000.0)
        return self._reply(prompt, max_sentences=max_sentences).split(" ")[0]

    async def complete(self, prompt: str, *, max_sentences: int | None = None) -> str:
        self.chaos.check_fail("llm_complete")
        await asyncio.sleep((self.complete_ms * self._rng.uniform(0.9, 1.15)) / 1000.0)
        return self._reply(prompt, max_sentences=max_sentences)


@dataclass
class SimulatedTTS:
    name: str = "simulated-tts (Sonic-shaped)"
    first_chunk_ms: float = 95.0
    complete_ms: float = 300.0
    chaos: ChaosConfig = field(default_factory=ChaosConfig)
    _rng: random.Random = field(default_factory=lambda: random.Random(13))

    async def first_chunk(self, text: str) -> bytes:
        self.chaos.check_fail("tts_first_chunk")
        await self.chaos.maybe_delay("tts_first_chunk", self._rng)
        await asyncio.sleep((self.first_chunk_ms * self._rng.uniform(0.85, 1.2)) / 1000.0)
        # A real adapter returns Opus/PCM frames; the simulation returns a
        # small deterministic stand-in chunk tagged with the text length.
        return b"VOXCHUNK:" + str(len(text)).encode()

    async def synthesise(self, text: str) -> bytes:
        self.chaos.check_fail("tts_complete")
        await asyncio.sleep((self.complete_ms * self._rng.uniform(0.9, 1.1)) / 1000.0)
        return b"VOXAUDIO:" + str(len(text)).encode()


# --------------------------------------------------------------------------
# Real adapters — constructed only when selected; keys from environment
# --------------------------------------------------------------------------
class DeepgramSTT:
    """Deepgram Nova-3 streaming STT adapter (requires DEEPGRAM_API_KEY)."""

    name = "deepgram-nova-3"

    def __init__(self) -> None:
        if not os.environ.get("DEEPGRAM_API_KEY"):
            raise RuntimeError("DEEPGRAM_API_KEY is not set")
        # The live websocket client is created per session in production use;
        # see docs/architecture.md for the streaming wiring.

    async def transcribe(self, audio: bytes, *, transcript_hint: str) -> str:  # pragma: no cover
        raise NotImplementedError("wire the Deepgram live client per docs/architecture.md")


class OpenAIRealtimeLLM:
    """OpenAI Realtime API adapter (requires OPENAI_API_KEY)."""

    name = "openai-gpt-realtime"

    def __init__(self) -> None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set")

    async def first_token(self, prompt: str, *, max_sentences: int | None = None) -> str:  # pragma: no cover
        raise NotImplementedError("wire the Realtime websocket session per docs/architecture.md")

    async def complete(self, prompt: str, *, max_sentences: int | None = None) -> str:  # pragma: no cover
        raise NotImplementedError("wire the Realtime websocket session per docs/architecture.md")


class CartesiaTTS:
    """Cartesia Sonic TTS adapter (requires CARTESIA_API_KEY)."""

    name = "cartesia-sonic"

    def __init__(self) -> None:
        if not os.environ.get("CARTESIA_API_KEY"):
            raise RuntimeError("CARTESIA_API_KEY is not set")

    async def first_chunk(self, text: str) -> bytes:  # pragma: no cover
        raise NotImplementedError("wire the Cartesia streaming client per docs/architecture.md")

    async def synthesise(self, text: str) -> bytes:  # pragma: no cover
        raise NotImplementedError("wire the Cartesia streaming client per docs/architecture.md")

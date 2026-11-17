"""Graceful degradation — when the budget can't be met, get *less fancy*,
never *silent*.

The ladder (best to last resort):

    L0 FULL_VOICE       streaming STT -> LLM -> streaming TTS, full replies
    L1 SHORT_REPLIES    same pipeline, LLM capped to short replies and a
                        faster/cheaper model tier — buys back LLM + TTS time
    L2 TEXT_ONLY        TTS dropped (the slowest/least reliable stage in
                        many stacks); the user reads a streamed text reply
    L3 CACHED_FALLBACK  LLM dropped too; a cached, pre-synthesised filler /
                        holding response is played instantly while the real
                        answer is computed off the critical path
    L4 SAFE_FAIL        everything critical failed: preserve the transcript,
                        apologise in text, log the turn for replay — never
                        hang up, never spin forever

Movement rules:
- A stage timeout or an open circuit breaker drops the ladder immediately.
- Two consecutive turns over the total budget drop one level (hysteresis:
  one bad turn is jitter, two is a trend).
- Five consecutive healthy turns climb one level back up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from .budget import TOTAL_BUDGET_MS, TurnTiming


class Mode(IntEnum):
    FULL_VOICE = 0
    SHORT_REPLIES = 1
    TEXT_ONLY = 2
    CACHED_FALLBACK = 3
    SAFE_FAIL = 4

    @property
    def label(self) -> str:
        return self.name.lower()


CACHED_FALLBACK_PHRASES = (
    "Give me just a second on that.",
    "Working on it — one moment.",
    "Let me check that for you.",
)


@dataclass
class DegradationManager:
    mode: Mode = Mode.FULL_VOICE
    _over_budget_streak: int = 0
    _healthy_streak: int = 0
    _fallback_idx: int = 0
    history: list[str] = field(default_factory=list)

    # -- turn outcome -----------------------------------------------------
    def observe_turn(self, timing: TurnTiming, *, failed_stage: str | None = None) -> Mode:
        """Update the ladder from one finished turn; returns the current mode."""
        if failed_stage is not None:
            # A hard failure degrades immediately — no waiting for a streak.
            self._drop(2 if failed_stage in ("stt", "llm_first_token") else 1,
                      reason=f"{failed_stage} failed")
            self._over_budget_streak = 0
            self._healthy_streak = 0
            return self.mode

        if timing.first_audio_ms > TOTAL_BUDGET_MS:
            self._over_budget_streak += 1
            self._healthy_streak = 0
            if self._over_budget_streak >= 2:
                self._drop(1, reason="two consecutive turns over the 800 ms budget")
                self._over_budget_streak = 0
        else:
            self._over_budget_streak = 0
            self._healthy_streak += 1
            if self._healthy_streak >= 5 and self.mode > Mode.FULL_VOICE:
                self._climb(reason="five consecutive healthy turns")
                self._healthy_streak = 0
        return self.mode

    def force(self, mode: Mode, reason: str = "manual override") -> None:
        self.mode = mode
        self.history.append(f"forced -> {mode.label}: {reason}")

    # -- behaviour per mode -------------------------------------------------
    @property
    def use_tts(self) -> bool:
        return self.mode <= Mode.SHORT_REPLIES

    @property
    def use_llm(self) -> bool:
        return self.mode <= Mode.TEXT_ONLY

    @property
    def max_reply_sentences(self) -> int | None:
        return 1 if self.mode is Mode.SHORT_REPLIES else None

    def next_fallback_phrase(self) -> str:
        phrase = CACHED_FALLBACK_PHRASES[self._fallback_idx % len(CACHED_FALLBACK_PHRASES)]
        self._fallback_idx += 1
        return phrase

    # -- internals ------------------------------------------------------------
    def _drop(self, levels: int, *, reason: str) -> None:
        new = Mode(min(int(self.mode) + levels, int(Mode.SAFE_FAIL)))
        if new != self.mode:
            self.history.append(f"{self.mode.label} -> {new.label}: {reason}")
            self.mode = new

    def _climb(self, *, reason: str) -> None:
        new = Mode(max(int(self.mode) - 1, int(Mode.FULL_VOICE)))
        if new != self.mode:
            self.history.append(f"{self.mode.label} -> {new.label}: {reason}")
            self.mode = new

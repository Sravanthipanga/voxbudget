"""The latency budget — the heart of VoxBudget.

A conversational voice agent feels "real-time" only if the gap between the
user finishing speaking and the first audible reply stays under roughly
800 ms (human turn-taking gaps average ~200 ms; beyond ~1 s the user starts
talking over the agent). VoxBudget decomposes that 800 ms end-to-end target
into a per-stage budget, measures every stage against it on every turn, and
feeds the overruns into the degradation ladder.

Budget to *first audible response* (the number users actually feel):

    capture            20 ms   mic framing (20 ms Opus-style frames)
    vad_endpointing    80 ms   silence detection / smart turn-taking
    network_ingress    30 ms   WebSocket/WebRTC ingress + jitter buffer
    stt               200 ms   streaming speech-to-text to final transcript
    llm_first_token   250 ms   LLM time-to-first-token (streaming)
    tts_first_chunk   150 ms   TTS time-to-first-audio-chunk (streaming)
    network_egress     20 ms   first audio chunk back to the client
    ------------------------
    subtotal          750 ms
    reserve            50 ms   slack for jitter / GC / scheduling
    TOTAL             800 ms

Stages after first audio (LLM completion, remaining TTS) stream *while the
user is already listening*, so they are budgeted separately and never block
the perceived latency.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from enum import StrEnum


class Stage(StrEnum):
    CAPTURE = "capture"
    VAD = "vad_endpointing"
    NETWORK_INGRESS = "network_ingress"
    STT = "stt"
    LLM_FIRST_TOKEN = "llm_first_token"
    LLM_COMPLETE = "llm_complete"
    TTS_FIRST_CHUNK = "tts_first_chunk"
    TTS_COMPLETE = "tts_complete"
    NETWORK_EGRESS = "network_egress"


# Stages that sit on the critical path to first audible response.
CRITICAL_PATH: tuple[Stage, ...] = (
    Stage.CAPTURE,
    Stage.VAD,
    Stage.NETWORK_INGRESS,
    Stage.STT,
    Stage.LLM_FIRST_TOKEN,
    Stage.TTS_FIRST_CHUNK,
    Stage.NETWORK_EGRESS,
)

DEFAULT_BUDGETS_MS: dict[Stage, float] = {
    Stage.CAPTURE: 20.0,
    Stage.VAD: 80.0,
    Stage.NETWORK_INGRESS: 30.0,
    Stage.STT: 200.0,
    Stage.LLM_FIRST_TOKEN: 250.0,
    Stage.LLM_COMPLETE: 1_500.0,   # off critical path — streams during playback
    Stage.TTS_FIRST_CHUNK: 150.0,
    Stage.TTS_COMPLETE: 1_000.0,   # off critical path — streams during playback
    Stage.NETWORK_EGRESS: 20.0,
}

TOTAL_BUDGET_MS = 800.0
RESERVE_MS = 50.0


@dataclass(frozen=True)
class StageBudget:
    stage: Stage
    budget_ms: float
    # A stage is killed at budget * timeout_factor — never allowed to run
    # unbounded, because one hung stage would consume the whole turn.
    timeout_factor: float = 2.0

    @property
    def timeout_ms(self) -> float:
        return self.budget_ms * self.timeout_factor


@dataclass
class LatencyBudget:
    """Per-stage budgets + the total end-to-end target."""

    stages: dict[Stage, StageBudget] = field(
        default_factory=lambda: {
            stage: StageBudget(stage, ms) for stage, ms in DEFAULT_BUDGETS_MS.items()
        }
    )
    total_ms: float = TOTAL_BUDGET_MS
    reserve_ms: float = RESERVE_MS

    def budget_ms(self, stage: Stage) -> float:
        return self.stages[stage].budget_ms

    def timeout_ms(self, stage: Stage) -> float:
        return self.stages[stage].timeout_ms

    @property
    def critical_path_subtotal_ms(self) -> float:
        return sum(self.stages[s].budget_ms for s in CRITICAL_PATH)

    def as_table(self) -> list[dict]:
        rows = []
        for stage in Stage:
            b = self.stages[stage]
            rows.append(
                {
                    "stage": stage.value,
                    "budget_ms": b.budget_ms,
                    "timeout_ms": b.timeout_ms,
                    "on_critical_path": stage in CRITICAL_PATH,
                }
            )
        return rows


@dataclass
class StageTiming:
    stage: Stage
    elapsed_ms: float
    budget_ms: float
    timed_out: bool = False
    skipped: bool = False

    @property
    def over_budget(self) -> bool:
        return not self.skipped and self.elapsed_ms > self.budget_ms

    @property
    def utilisation(self) -> float:
        return 0.0 if self.skipped else self.elapsed_ms / self.budget_ms


@dataclass
class TurnTiming:
    """All stage timings for one conversational turn."""

    timings: dict[Stage, StageTiming] = field(default_factory=dict)

    def record(self, timing: StageTiming) -> None:
        self.timings[timing.stage] = timing

    @property
    def first_audio_ms(self) -> float:
        """Time to first audible response — sum of critical-path stages."""
        return sum(
            t.elapsed_ms for s, t in self.timings.items() if s in CRITICAL_PATH and not t.skipped
        )

    @property
    def total_ms(self) -> float:
        return sum(t.elapsed_ms for t in self.timings.values() if not t.skipped)

    @property
    def over_budget_stages(self) -> list[Stage]:
        return [s for s, t in self.timings.items() if t.over_budget or t.timed_out]

    @property
    def worst_stage(self) -> Stage | None:
        measured = [t for t in self.timings.values() if not t.skipped]
        return max(measured, key=lambda t: t.utilisation).stage if measured else None

    def to_dict(self) -> dict:
        return {
            "first_audio_ms": round(self.first_audio_ms, 2),
            "total_ms": round(self.total_ms, 2),
            "over_budget_stages": [s.value for s in self.over_budget_stages],
            "worst_stage": self.worst_stage.value if self.worst_stage else None,
            "stages": {
                s.value: {
                    "elapsed_ms": round(t.elapsed_ms, 2),
                    "budget_ms": t.budget_ms,
                    "timed_out": t.timed_out,
                    "skipped": t.skipped,
                    "over_budget": t.over_budget,
                }
                for s, t in self.timings.items()
            },
        }


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile — honest for the small samples a demo sees."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, -(-int(pct * len(ordered)) // 100))  # ceil(pct/100 * n)
    return ordered[min(rank, len(ordered)) - 1]


@dataclass
class StageStats:
    """Rolling per-stage latency distribution (p50/p95/p99)."""

    samples: dict[Stage, list[float]] = field(default_factory=dict)
    first_audio_samples: list[float] = field(default_factory=list)
    _max_samples: int = 500

    def add_turn(self, timing: TurnTiming) -> None:
        for stage, t in timing.timings.items():
            if t.skipped:
                continue
            bucket = self.samples.setdefault(stage, [])
            bucket.append(t.elapsed_ms)
            del bucket[: -self._max_samples]
        self.first_audio_samples.append(timing.first_audio_ms)
        del self.first_audio_samples[: -self._max_samples]

    @staticmethod
    def _summary(values: list[float]) -> dict:
        if not values:
            return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0}
        return {
            "count": len(values),
            "p50": round(percentile(values, 50), 2),
            "p95": round(percentile(values, 95), 2),
            "p99": round(percentile(values, 99), 2),
            "mean": round(statistics.fmean(values), 2),
        }

    def summary(self) -> dict:
        return {
            "first_audio": self._summary(self.first_audio_samples),
            "stages": {s.value: self._summary(v) for s, v in self.samples.items()},
        }

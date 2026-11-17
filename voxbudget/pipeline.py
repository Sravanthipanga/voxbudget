"""The streaming pipeline — Pipecat/LiveKit-style stage processors in a row.

One conversational turn flows:

    capture -> VAD/endpointing -> network ingress -> STT -> LLM (first token,
    then completion streaming *behind* first audio) -> TTS (first chunk, then
    the rest) -> network egress

Design points that make it a real-time system and not a request/response app:

- **Streaming everywhere.** The turn is considered "answered" at first audio,
  not at completion. LLM completion and full TTS run after first audio is
  already on its way to the user.
- **Every stage is timed and deadline-bound.** Each stage runs through
  ``run_with_timeout`` with its own circuit breaker; timings land in a
  ``TurnTiming`` that the budget engine and the dashboard consume.
- **Barge-in.** ``barge_in()`` cancels the in-flight turn's playback stages —
  when the user starts talking, the agent stops talking.
- **Failure = degrade, not die.** A stage timeout/failure marks the turn,
  notifies the degradation manager, and the turn still completes at whatever
  ladder level is now active (text-only, cached fallback, or safe fail).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from .budget import LatencyBudget, Stage, StageStats, StageTiming, TurnTiming
from .degradation import DegradationManager, Mode
from .providers import ChaosConfig, SimulatedLLM, SimulatedSTT, SimulatedTTS
from .resilience import CircuitBreaker, CircuitOpenError, StageTimeoutError, run_with_timeout


@dataclass
class TurnRequest:
    # In production these are audio frames; the simulated pipeline carries the
    # bytes plus the ground-truth hint a real STT would have produced.
    audio: bytes = b""
    transcript_hint: str = ""
    text_input: str | None = None  # typed input skips capture/VAD/STT


@dataclass
class TurnEvent:
    kind: str  # stage | transcript | token | audio_chunk | degradation | done | error
    data: dict = field(default_factory=dict)


@dataclass
class TurnResult:
    transcript: str
    reply_text: str
    timing: TurnTiming
    mode: Mode
    failed_stage: str | None = None
    audio_bytes: int = 0

    def to_dict(self) -> dict:
        return {
            "transcript": self.transcript,
            "reply_text": self.reply_text,
            "mode": self.mode.label,
            "failed_stage": self.failed_stage,
            "audio_bytes": self.audio_bytes,
            "timing": self.timing.to_dict(),
        }


class VoicePipeline:
    def __init__(
        self,
        budget: LatencyBudget | None = None,
        chaos: ChaosConfig | None = None,
    ) -> None:
        self.budget = budget or LatencyBudget()
        self.chaos = chaos or ChaosConfig()
        self.stt = SimulatedSTT(chaos=self.chaos)
        self.llm = SimulatedLLM(chaos=self.chaos)
        self.tts = SimulatedTTS(chaos=self.chaos)
        self.degradation = DegradationManager()
        self.stats = StageStats()
        self.breakers = {
            Stage.STT: CircuitBreaker("stt"),
            Stage.LLM_FIRST_TOKEN: CircuitBreaker("llm"),
            Stage.TTS_FIRST_CHUNK: CircuitBreaker("tts"),
        }
        self._current_turn: asyncio.Task | None = None

    # ------------------------------------------------------------------
    async def _timed_stage(
        self,
        timing: TurnTiming,
        stage: Stage,
        coro_factory,
        *,
        breaker: CircuitBreaker | None = None,
    ):
        """Run one stage under its budget deadline; record its timing either way."""
        start = time.perf_counter()
        try:
            result = await run_with_timeout(
                coro_factory, stage=stage.value,
                timeout_ms=self.budget.timeout_ms(stage), breaker=breaker,
            )
        except (StageTimeoutError, CircuitOpenError):
            elapsed = (time.perf_counter() - start) * 1000.0
            timing.record(StageTiming(stage, elapsed, self.budget.budget_ms(stage),
                                      timed_out=True))
            raise
        except Exception as exc:
            # Tag provider failures with their stage so the caller can degrade
            # the right rung of the ladder.
            if not hasattr(exc, "stage"):
                exc.stage = stage.value  # type: ignore[attr-defined]
            elapsed = (time.perf_counter() - start) * 1000.0
            timing.record(StageTiming(stage, elapsed, self.budget.budget_ms(stage)))
            raise
        elapsed = (time.perf_counter() - start) * 1000.0
        timing.record(StageTiming(stage, elapsed, self.budget.budget_ms(stage)))
        return result

    @staticmethod
    async def _instant_stage(timing: TurnTiming, budget: LatencyBudget,
                             stage: Stage, work_ms: float) -> None:
        """Local stages (capture/VAD/transport) are simulated sleeps — in
        production these are measured around real frame handling."""
        start = time.perf_counter()
        await asyncio.sleep(work_ms / 1000.0)
        elapsed = (time.perf_counter() - start) * 1000.0
        timing.record(StageTiming(stage, elapsed, budget.budget_ms(stage)))

    # ------------------------------------------------------------------
    async def run_turn(self, request: TurnRequest) -> TurnResult:
        async for event in self.stream_turn(request):
            if event.kind == "done":
                return event.data["result"]
        raise RuntimeError("turn ended without a result")  # pragma: no cover

    async def stream_turn(self, request: TurnRequest) -> AsyncIterator[TurnEvent]:
        timing = TurnTiming()
        mode = self.degradation.mode
        failed_stage: str | None = None
        transcript = request.text_input or ""
        reply = ""
        audio_len = 0

        async def stage_event(stage: Stage) -> TurnEvent:
            t = timing.timings[stage]
            return TurnEvent("stage", {"stage": stage.value,
                                       "elapsed_ms": round(t.elapsed_ms, 2),
                                       "budget_ms": t.budget_ms,
                                       "over_budget": t.over_budget})

        try:
            if request.text_input is None:
                await self._instant_stage(timing, self.budget, Stage.CAPTURE, 14.0)
                yield await stage_event(Stage.CAPTURE)
                await self._instant_stage(timing, self.budget, Stage.VAD, 55.0)
                yield await stage_event(Stage.VAD)
                await self._instant_stage(timing, self.budget, Stage.NETWORK_INGRESS, 18.0)
                yield await stage_event(Stage.NETWORK_INGRESS)

                transcript = await self._timed_stage(
                    timing, Stage.STT,
                    lambda: self.stt.transcribe(request.audio,
                                                transcript_hint=request.transcript_hint),
                    breaker=self.breakers[Stage.STT],
                )
                yield await stage_event(Stage.STT)
            else:
                for skipped in (Stage.CAPTURE, Stage.VAD, Stage.NETWORK_INGRESS, Stage.STT):
                    timing.record(StageTiming(skipped, 0.0, self.budget.budget_ms(skipped),
                                              skipped=True))
            yield TurnEvent("transcript", {"text": transcript})

            if not self.degradation.use_llm:
                # L3/L4: cached fallback — instant, pre-synthesised in production.
                reply = self.degradation.next_fallback_phrase()
                timing.record(StageTiming(Stage.LLM_FIRST_TOKEN, 0.0,
                                          self.budget.budget_ms(Stage.LLM_FIRST_TOKEN),
                                          skipped=True))
                timing.record(StageTiming(Stage.LLM_COMPLETE, 0.0,
                                          self.budget.budget_ms(Stage.LLM_COMPLETE),
                                          skipped=True))
            else:
                max_sent = self.degradation.max_reply_sentences
                await self._timed_stage(
                    timing, Stage.LLM_FIRST_TOKEN,
                    lambda: self.llm.first_token(transcript, max_sentences=max_sent),
                    breaker=self.breakers[Stage.LLM_FIRST_TOKEN],
                )
                yield await stage_event(Stage.LLM_FIRST_TOKEN)
                yield TurnEvent("token", {"text": "…"})
                reply = await self._timed_stage(
                    timing, Stage.LLM_COMPLETE,
                    lambda: self.llm.complete(transcript, max_sentences=max_sent),
                )
                yield TurnEvent("reply", {"text": reply})

            if self.degradation.use_tts:
                chunk = await self._timed_stage(
                    timing, Stage.TTS_FIRST_CHUNK,
                    lambda: self.tts.first_chunk(reply),
                    breaker=self.breakers[Stage.TTS_FIRST_CHUNK],
                )
                audio_len += len(chunk)
                yield await stage_event(Stage.TTS_FIRST_CHUNK)
                yield TurnEvent("audio_chunk", {"bytes": len(chunk)})
                full = await self._timed_stage(
                    timing, Stage.TTS_COMPLETE, lambda: self.tts.synthesise(reply))
                audio_len += len(full)
            else:
                for skipped in (Stage.TTS_FIRST_CHUNK, Stage.TTS_COMPLETE):
                    timing.record(StageTiming(skipped, 0.0, self.budget.budget_ms(skipped),
                                              skipped=True))

            await self._instant_stage(timing, self.budget, Stage.NETWORK_EGRESS, 12.0)
            yield await stage_event(Stage.NETWORK_EGRESS)

        except (StageTimeoutError, CircuitOpenError, RuntimeError) as exc:
            failed_stage = getattr(exc, "stage", None) or self._infer_failed_stage(timing)
            # The turn still completes — at a degraded level. If we have no
            # reply at all, fall back to a cached phrase / safe-fail text.
            if not reply:
                if self.degradation.mode >= Mode.CACHED_FALLBACK or failed_stage == "stt":
                    reply = ("I couldn't catch that clearly — could you say it once more?"
                             if failed_stage == "stt" else
                             self.degradation.next_fallback_phrase())
                else:
                    reply = self.degradation.next_fallback_phrase()
            yield TurnEvent("error", {"stage": failed_stage, "message": str(exc)})

        new_mode = self.degradation.observe_turn(timing, failed_stage=failed_stage)
        if new_mode != mode:
            yield TurnEvent("degradation", {"from": mode.label, "to": new_mode.label,
                                            "history": self.degradation.history[-1:]})
        self.stats.add_turn(timing)
        result = TurnResult(transcript=transcript, reply_text=reply, timing=timing,
                            mode=new_mode, failed_stage=failed_stage, audio_bytes=audio_len)
        yield TurnEvent("done", {"result": result})

    @staticmethod
    def _infer_failed_stage(timing: TurnTiming) -> str | None:
        for stage, t in timing.timings.items():
            if t.timed_out:
                return stage.value
        return None

    # ------------------------------------------------------------------
    async def barge_in(self) -> bool:
        """User started talking over playback: cancel the in-flight turn."""
        if self._current_turn and not self._current_turn.done():
            self._current_turn.cancel()
            return True
        return False

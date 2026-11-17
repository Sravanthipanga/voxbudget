# VoxBudget — architecture

```
Browser (mic via Web Speech API, playback via SpeechSynthesis)
   │  WebSocket /ws/voice  (REST /turn for non-streaming clients)
   ▼
FastAPI app (voxbudget/api.py)
   │
   ▼
VoicePipeline (voxbudget/pipeline.py) — Pipecat/LiveKit-style stage chain
   capture → VAD → ingress → STT → LLM → TTS → egress
   │            │              │       │       │
   │            └─ every stage: run_with_timeout (deadline = 2× budget)
   │                          + per-provider CircuitBreaker
   ▼
LatencyBudget (budget.py)      DegradationManager (degradation.py)
per-stage budgets, TurnTiming,  L0 full voice → L1 short replies →
p50/p95/p99 StageStats          L2 text only → L3 cached fallback → L4 safe fail
   │
   ▼
Providers (providers.py) — simulated by default (no keys, deterministic
latency shapes); real adapters: Deepgram Nova-3 (STT), OpenAI Realtime
(LLM), Cartesia Sonic (TTS), ElevenLabs Flash as alternate TTS.
```

## Wiring a real provider

1. `pip install "voxbudget[realtime,observability]"`
2. Set the key: `DEEPGRAM_API_KEY` / `OPENAI_API_KEY` / `CARTESIA_API_KEY`.
3. Swap the simulated provider in `VoicePipeline.__init__` for the adapter —
   the pipeline only depends on the `STTProvider` / `LLMProvider` /
   `TTSProvider` protocols, and the budget/timeout/breaker wrapping is
   provider-agnostic.

For a fully-managed transport, the same stage chain maps 1:1 onto a
**LiveKit Agents** worker (STT/LLM/TTS plugins) or a **Pipecat** frame
pipeline (processors in series); VoxBudget's contribution — the budget,
deadlines, and degradation ladder — ports as a wrapper around either.

## Observability

- `GET /metrics` — Prometheus text: turn counters, failure counter,
  degradation mode, first-audio p50/p95/p99, per-stage p95.
- `GET /stats` — JSON percentiles + breaker states + degradation history.
- The `observability` extra adds OpenTelemetry; in production each stage is
  a span (`stage`, `budget_ms`, `over_budget`) under one trace per turn, so a
  slow turn is a waterfall in Jaeger/Tempo exactly like the dashboard's.

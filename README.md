# 🎙️ VoxBudget

**A real-time multimodal voice assistant pipeline that treats latency as a budget — decomposed, measured, enforced, and gracefully spent.**

Talk to it in the browser (real voice in/out via the Web Speech API), watch every stage of the turn race its budget on a live waterfall, then break a stage in the chaos panel and watch the system degrade gracefully instead of dying.

Built on the 2025–26 real-time voice stack: **FastAPI + WebSockets** streaming, a **Pipecat / LiveKit Agents-style** stage pipeline, provider interfaces shaped on **Deepgram Nova-3** (STT), **OpenAI Realtime / gpt-realtime** (LLM), and **Cartesia Sonic / ElevenLabs Flash** (TTS), with **Prometheus** metrics and **OpenTelemetry-ready** tracing. Simulated providers run the whole thing with **zero API keys**.

![Python 3.12](https://img.shields.io/badge/python-3.12-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688) ![Tests](https://img.shields.io/badge/tests-40%20passing-brightgreen) ![License MIT](https://img.shields.io/badge/license-MIT-green)

---

## The latency budget

A voice agent feels real-time only if the gap from end-of-speech to **first audible response** stays under ~800 ms. VoxBudget decomposes that target and holds every stage to it:

| Stage | Budget | Timeout |
|---|---|---|
| Capture (20 ms frames) | 20 ms | 40 ms |
| VAD / endpointing (smart turn-taking) | 80 ms | 160 ms |
| Network ingress + jitter buffer | 30 ms | 60 ms |
| STT — streaming to final transcript | 200 ms | 400 ms |
| LLM — time to first token | 250 ms | 500 ms |
| TTS — time to first audio chunk | 150 ms | 300 ms |
| Network egress | 20 ms | 40 ms |
| **Subtotal** | **750 ms** | |
| Reserve (jitter / GC / scheduling) | 50 ms | |
| **Total to first audio** | **800 ms** | |

LLM/TTS *completion* is budgeted separately — it streams behind playback and never blocks perceived latency. Full rationale in [docs/latency-budget.md](docs/latency-budget.md).

## Timeout handling

- **Every stage runs under a deadline** (`budget × 2`) via `asyncio.wait_for`; a hung provider is cancelled, never waited on.
- **Cancellation propagates** — a dead STT means the LLM is never called for that turn.
- **Per-provider circuit breakers** fail fast after 3 consecutive failures, half-open probe after 30 s.
- **No retries on the critical path** — a retry costs a second full stage latency; the system degrades instead.

## Graceful degradation ladder

| Level | Mode | Behaviour |
|---|---|---|
| L0 | `full_voice` | Streaming STT → LLM → streaming TTS, full replies |
| L1 | `short_replies` | Same pipeline, replies capped to one sentence (buys back LLM + TTS time) |
| L2 | `text_only` | TTS dropped; streamed text reply |
| L3 | `cached_fallback` | Instant cached holding phrase while the real answer computes off-path |
| L4 | `safe_fail` | Transcript preserved, text apology, turn logged — never silence, never a hang |

A stage failure drops the ladder **immediately**; two consecutive over-budget turns drop one level (one bad turn is jitter, two is a trend); five healthy turns climb back up.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn voxbudget.api:app --reload
# open http://localhost:8000
```

Or with Docker: `docker build -t voxbudget . && docker run -p 8000:8000 voxbudget`

**Try the chaos panel:** inject +900 ms of STT delay → watch the STT stage time out at 400 ms, the turn complete with fallback wording, and the ladder drop. Fail TTS → replies keep flowing as text.

## API

| Route | What |
|---|---|
| `GET /` | Live dashboard (voice, waterfall, chaos, stats) |
| `WS /ws/voice` | Streaming turn protocol — stage/transcript/reply/audio/degradation events as they happen |
| `POST /turn` | One turn, JSON in/out |
| `GET /budget` | The decomposed budget as JSON |
| `GET /stats` | Per-stage p50/p95/p99, breaker states, degradation history |
| `GET /metrics` | Prometheus text |
| `POST /chaos` | Fault injection (delay / fail a stage, or clear) |

## Real providers

Simulated providers are deterministic, latency-shaped stand-ins. Adapters for **Deepgram Nova-3**, **OpenAI Realtime**, and **Cartesia Sonic** are included (`pip install "voxbudget[realtime]"`, keys from environment) behind the same protocols — swapping one in changes nothing about the budget, timeout, or degradation behaviour. See [docs/architecture.md](docs/architecture.md).

## Tests

```bash
.venv/bin/pytest -q   # 40 tests: budget math, timeouts, breakers, ladder, pipeline, API/WS
```

## License

MIT © Sravanthi Panga

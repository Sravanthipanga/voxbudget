# VoxBudget — the latency budget, in detail

## Why 800 ms?

Human conversation runs on ~200 ms turn-taking gaps. Research on voice
interfaces consistently finds users start perceiving a system as "slow" past
~500 ms and start talking over it past ~1 s. VoxBudget targets **800 ms from
end-of-user-speech to first audible response (p95)** — ambitious enough to
feel responsive, honest enough to be achievable with real cloud providers.

## The decomposition

| # | Stage | Budget | Timeout (2×) | What eats the time | How real systems shave it |
|---|-------|--------|--------------|--------------------|---------------------------|
| 1 | Capture | 20 ms | 40 ms | 20 ms audio frames (Opus), mic buffering | Frame-size vs overhead trade-off; never buffer whole utterances |
| 2 | VAD / endpointing | 80 ms | 160 ms | Deciding the user actually finished — silence threshold + smart-turn model | Silero VAD + turn-detection models cut the classic 500–700 ms silence wait to <100 ms |
| 3 | Network ingress | 30 ms | 60 ms | WebRTC/WebSocket ingress, jitter buffer | Edge PoPs, UDP/WebRTC over TCP, small jitter buffers (real-time ≠ reliable-ordered) |
| 4 | STT | 200 ms | 400 ms | Streaming ASR to final transcript (Deepgram Nova-3 class) | Streaming partials; the LLM can start on a stable partial instead of the final |
| 5 | LLM first token | 250 ms | 500 ms | Time-to-first-token, GPT-Realtime / fast-tier models | Streaming, small system prompts, speculative/prefill caching, fast model tier under degradation |
| 6 | TTS first chunk | 150 ms | 300 ms | Time-to-first-audio (Cartesia Sonic / ElevenLabs Flash class) | Sentence-chunked synthesis: synthesise sentence 1 while the LLM writes sentence 2 |
| 7 | Network egress | 20 ms | 40 ms | First audio chunk back to the client | Same edge path as ingress |
|   | **Subtotal** | **750 ms** | | | |
|   | Reserve | 50 ms | | Jitter, GC pauses, scheduling | The reserve is spent by reality whether you budget it or not |
|   | **Total** | **800 ms** | | | |

Off the critical path (streaming behind playback, budgeted separately):
LLM completion 1,500 ms timeout-budget, TTS completion 1,000 ms. The user is
already listening — these affect total turn time, not perceived latency.

## The rules the numbers impose

1. **Nothing on the critical path may wait for a whole anything** — whole
   utterance, whole reply, whole synthesis. Streaming or it doesn't ship.
2. **Every stage gets a deadline = 2× its budget.** A stage that overruns its
   budget is already hurting; one that passes 2× is hung, and is killed.
3. **Retries are banned on the critical path.** A retry costs a second full
   stage latency. Degrade instead (see README ladder).
4. **Measure percentiles, not averages.** Averages hide the tail users
   remember. VoxBudget tracks p50/p95/p99 per stage.
5. **Barge-in is a budget feature.** If the user interrupts, playback and
   pending synthesis are cancelled immediately — continuing to speak is the
   worst latency of all: answering a question nobody is still asking.

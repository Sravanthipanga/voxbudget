import asyncio

from voxbudget.degradation import Mode
from voxbudget.pipeline import TurnRequest, VoicePipeline
from voxbudget.providers import ChaosConfig


def _run(pipeline, request):
    return asyncio.run(pipeline.run_turn(request))


def test_voice_turn_completes_within_budget():
    p = VoicePipeline()
    r = _run(p, TurnRequest(audio=b"a", transcript_hint="how does the budget work"))
    assert r.transcript == "how does the budget work"
    assert r.reply_text
    assert r.failed_stage is None
    assert r.mode is Mode.FULL_VOICE
    assert r.timing.first_audio_ms < 800
    assert r.audio_bytes > 0


def test_text_input_skips_voice_stages():
    p = VoicePipeline()
    r = _run(p, TurnRequest(text_input="hello"))
    assert r.timing.timings["stt"].skipped if "stt" in r.timing.timings else True
    from voxbudget.budget import Stage
    assert r.timing.timings[Stage.STT].skipped
    assert r.reply_text


def test_streaming_events_arrive_in_order():
    p = VoicePipeline()

    async def collect():
        kinds = []
        async for e in p.stream_turn(TurnRequest(audio=b"a", transcript_hint="hi")):
            kinds.append(e.kind)
        return kinds

    kinds = asyncio.run(collect())
    assert kinds[0] == "stage"
    assert "transcript" in kinds
    assert kinds[-1] == "done"


def test_stt_timeout_degrades_and_turn_still_completes():
    chaos = ChaosConfig(extra_delay_ms={"stt": 5_000})
    p = VoicePipeline(chaos=chaos)
    r = _run(p, TurnRequest(audio=b"a", transcript_hint="hello"))
    assert r.failed_stage == "stt"
    assert r.reply_text  # safe fallback wording, never silence
    assert r.mode >= Mode.TEXT_ONLY


def test_llm_failure_uses_cached_fallback():
    chaos = ChaosConfig(fail_stages={"llm_first_token"})
    p = VoicePipeline(chaos=chaos)
    r = _run(p, TurnRequest(audio=b"a", transcript_hint="hello"))
    assert r.failed_stage == "llm_first_token"
    assert r.reply_text


def test_tts_failure_degrades_but_reply_exists():
    chaos = ChaosConfig(fail_stages={"tts_first_chunk"})
    p = VoicePipeline(chaos=chaos)
    r = _run(p, TurnRequest(audio=b"a", transcript_hint="hello"))
    assert r.failed_stage == "tts_first_chunk"
    assert r.reply_text


def test_text_only_mode_skips_tts():
    p = VoicePipeline()
    p.degradation.force(Mode.TEXT_ONLY)
    r = _run(p, TurnRequest(audio=b"a", transcript_hint="hello"))
    from voxbudget.budget import Stage
    assert r.timing.timings[Stage.TTS_FIRST_CHUNK].skipped
    assert r.audio_bytes == 0


def test_cached_fallback_mode_skips_llm_and_tts():
    p = VoicePipeline()
    p.degradation.force(Mode.CACHED_FALLBACK)
    r = _run(p, TurnRequest(audio=b"a", transcript_hint="hello"))
    assert r.reply_text in (
        "Give me just a second on that.",
        "Working on it — one moment.",
        "Let me check that for you.",
    )


def test_stats_accumulate():
    p = VoicePipeline()
    _run(p, TurnRequest(audio=b"a", transcript_hint="one"))
    _run(p, TurnRequest(audio=b"a", transcript_hint="two"))
    s = p.stats.summary()
    assert s["first_audio"]["count"] == 2

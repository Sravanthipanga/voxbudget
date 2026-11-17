from voxbudget.budget import Stage, StageTiming, TurnTiming
from voxbudget.degradation import DegradationManager, Mode


def _timing(first_audio_ms: float) -> TurnTiming:
    t = TurnTiming()
    t.record(StageTiming(Stage.STT, first_audio_ms, 200.0))
    return t


def test_starts_at_full_voice():
    d = DegradationManager()
    assert d.mode is Mode.FULL_VOICE
    assert d.use_tts and d.use_llm


def test_failed_stt_drops_immediately_two_levels():
    d = DegradationManager()
    d.observe_turn(_timing(100), failed_stage="stt")
    assert d.mode is Mode.TEXT_ONLY


def test_failed_tts_drops_one_level():
    d = DegradationManager()
    d.observe_turn(_timing(100), failed_stage="tts_first_chunk")
    assert d.mode is Mode.SHORT_REPLIES


def test_two_over_budget_turns_drop_one_level():
    d = DegradationManager()
    d.observe_turn(_timing(900))
    assert d.mode is Mode.FULL_VOICE  # one bad turn is jitter
    d.observe_turn(_timing(950))
    assert d.mode is Mode.SHORT_REPLIES


def test_five_healthy_turns_climb_back():
    d = DegradationManager()
    d.force(Mode.TEXT_ONLY)
    for _ in range(5):
        d.observe_turn(_timing(300))
    assert d.mode is Mode.SHORT_REPLIES


def test_text_only_disables_tts_but_keeps_llm():
    d = DegradationManager()
    d.force(Mode.TEXT_ONLY)
    assert not d.use_tts and d.use_llm


def test_cached_fallback_disables_llm():
    d = DegradationManager()
    d.force(Mode.CACHED_FALLBACK)
    assert not d.use_llm and not d.use_tts
    assert d.next_fallback_phrase()


def test_short_replies_caps_sentences():
    d = DegradationManager()
    d.force(Mode.SHORT_REPLIES)
    assert d.max_reply_sentences == 1


def test_ladder_never_goes_below_safe_fail():
    d = DegradationManager()
    for _ in range(6):
        d.observe_turn(_timing(100), failed_stage="stt")
    assert d.mode is Mode.SAFE_FAIL

from voxbudget.budget import (
    CRITICAL_PATH, RESERVE_MS, TOTAL_BUDGET_MS, LatencyBudget, Stage,
    StageStats, StageTiming, TurnTiming, percentile,
)


def test_budget_sums_to_total():
    b = LatencyBudget()
    assert b.critical_path_subtotal_ms + RESERVE_MS == TOTAL_BUDGET_MS == 800.0


def test_every_stage_has_budget_and_timeout():
    b = LatencyBudget()
    for stage in Stage:
        assert b.budget_ms(stage) > 0
        assert b.timeout_ms(stage) == b.budget_ms(stage) * 2.0


def test_critical_path_excludes_completion_stages():
    assert Stage.LLM_COMPLETE not in CRITICAL_PATH
    assert Stage.TTS_COMPLETE not in CRITICAL_PATH
    assert Stage.STT in CRITICAL_PATH


def test_first_audio_sums_only_critical_path():
    t = TurnTiming()
    t.record(StageTiming(Stage.STT, 100.0, 200.0))
    t.record(StageTiming(Stage.LLM_COMPLETE, 900.0, 1500.0))
    assert t.first_audio_ms == 100.0
    assert t.total_ms == 1000.0


def test_over_budget_detection():
    t = TurnTiming()
    t.record(StageTiming(Stage.STT, 250.0, 200.0))
    assert t.over_budget_stages == [Stage.STT]
    assert t.worst_stage == Stage.STT


def test_skipped_stages_do_not_count():
    t = TurnTiming()
    t.record(StageTiming(Stage.TTS_FIRST_CHUNK, 0.0, 150.0, skipped=True))
    assert t.first_audio_ms == 0.0
    assert t.over_budget_stages == []


def test_percentile_nearest_rank():
    values = list(range(1, 101))
    assert percentile(values, 50) == 50
    assert percentile(values, 95) == 95
    assert percentile([], 95) == 0.0


def test_stage_stats_summary():
    stats = StageStats()
    t = TurnTiming()
    t.record(StageTiming(Stage.STT, 120.0, 200.0))
    stats.add_turn(t)
    s = stats.summary()
    assert s["stages"]["stt"]["count"] == 1
    assert s["first_audio"]["count"] == 1


def test_budget_table_shape():
    rows = LatencyBudget().as_table()
    assert len(rows) == len(Stage)
    assert all("timeout_ms" in r for r in rows)

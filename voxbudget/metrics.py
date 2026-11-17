"""Prometheus-text metrics — no extra dependency, scrape-ready."""

from __future__ import annotations

from .budget import StageStats


def render_prometheus(stats: StageStats, *, mode: str, mode_level: int = 0,
                      turns_total: int = 0, failures_total: int = 0) -> str:
    lines = [
        "# HELP voxbudget_turns_total Completed conversational turns",
        "# TYPE voxbudget_turns_total counter",
        f"voxbudget_turns_total {turns_total}",
        "# HELP voxbudget_turn_failures_total Turns with a failed/timed-out stage",
        "# TYPE voxbudget_turn_failures_total counter",
        f"voxbudget_turn_failures_total {failures_total}",
        "# HELP voxbudget_degradation_mode Current degradation ladder level (0=full voice)",
        "# TYPE voxbudget_degradation_mode gauge",
        f'voxbudget_degradation_mode{{mode="{mode}"}} {mode_level}',
    ]
    summary = stats.summary()
    fa = summary["first_audio"]
    for pct in ("p50", "p95", "p99"):
        lines += [
            f"# HELP voxbudget_first_audio_{pct}_ms Time to first audible response {pct}",
            f"# TYPE voxbudget_first_audio_{pct}_ms gauge",
            f"voxbudget_first_audio_{pct}_ms {fa[pct]}",
        ]
    for stage, s in summary["stages"].items():
        lines.append(f'voxbudget_stage_p95_ms{{stage="{stage}"}} {s["p95"]}')
    return "\n".join(lines) + "\n"

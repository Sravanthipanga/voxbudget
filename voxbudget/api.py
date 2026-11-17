"""FastAPI app — REST + WebSocket streaming, and the live dashboard.

Run:  uvicorn voxbudget.api:app --reload   (or: voxbudget)
Open: http://localhost:8000  — talk or type, watch the latency waterfall,
then use the chaos panel to break a stage and watch the ladder degrade.
"""

from __future__ import annotations

import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .budget import LatencyBudget
from .degradation import Mode
from .metrics import render_prometheus
from .pipeline import TurnRequest, VoicePipeline
from .providers import ChaosConfig

STATIC_DIR = Path(__file__).parent / "static"

chaos = ChaosConfig()
pipeline = VoicePipeline(budget=LatencyBudget(), chaos=chaos)
_turns_total = 0
_failures_total = 0

app = FastAPI(title="VoxBudget", version="1.0.0",
              description="Real-time multimodal voice pipeline with a latency budget, "
                          "per-stage timeouts, and graceful degradation.")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "mode": pipeline.degradation.mode.label,
            "turns": _turns_total}


@app.get("/budget")
async def budget() -> dict:
    b = pipeline.budget
    return {"total_ms": b.total_ms, "reserve_ms": b.reserve_ms,
            "critical_path_subtotal_ms": b.critical_path_subtotal_ms,
            "stages": b.as_table()}


@app.get("/stats")
async def stats() -> dict:
    return {"stats": pipeline.stats.summary(),
            "mode": pipeline.degradation.mode.label,
            "degradation_history": pipeline.degradation.history,
            "breakers": {s.value: b.state.value for s, b in pipeline.breakers.items()}}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    return render_prometheus(pipeline.stats, mode=pipeline.degradation.mode.label,
                             mode_level=int(pipeline.degradation.mode),
                             turns_total=_turns_total, failures_total=_failures_total)


class TurnIn(BaseModel):
    text: str = Field(min_length=1, max_length=2_000)
    as_voice: bool = True  # True = simulate the full voice path; False = typed input


@app.post("/turn")
async def turn(body: TurnIn) -> JSONResponse:
    global _turns_total, _failures_total
    request = TurnRequest(audio=b"simulated-audio", transcript_hint=body.text) \
        if body.as_voice else TurnRequest(text_input=body.text)
    result = await pipeline.run_turn(request)
    _turns_total += 1
    if result.failed_stage:
        _failures_total += 1
    return JSONResponse(result.to_dict())


class ChaosIn(BaseModel):
    stage: str | None = None          # stt | llm_first_token | tts_first_chunk ...
    extra_delay_ms: float = 0.0
    fail: bool = False
    clear: bool = False


@app.post("/chaos")
async def set_chaos(body: ChaosIn) -> dict:
    if body.clear:
        chaos.extra_delay_ms.clear()
        chaos.fail_stages.clear()
        return {"chaos": "cleared"}
    if body.stage:
        if body.extra_delay_ms > 0:
            chaos.extra_delay_ms[body.stage] = body.extra_delay_ms
        if body.fail:
            chaos.fail_stages.add(body.stage)
    return {"extra_delay_ms": chaos.extra_delay_ms,
            "fail_stages": sorted(chaos.fail_stages)}


@app.post("/mode/{mode_name}")
async def set_mode(mode_name: str) -> dict:
    try:
        mode = Mode[mode_name.upper()]
    except KeyError:
        return {"error": f"unknown mode '{mode_name}'",
                "modes": [m.label for m in Mode]}
    pipeline.degradation.force(mode, reason="dashboard override")
    return {"mode": mode.label}


@app.websocket("/ws/voice")
async def ws_voice(ws: WebSocket) -> None:
    """Streaming turn protocol: client sends {"text": "...", "as_voice": true},
    server streams stage / transcript / reply / audio_chunk / degradation /
    done events as they happen — never one big response at the end."""
    global _turns_total, _failures_total
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"kind": "error", "data": {"message": "invalid JSON"}})
                continue
            text = str(msg.get("text", "")).strip()
            if not text:
                continue
            request = TurnRequest(audio=b"simulated-audio", transcript_hint=text) \
                if msg.get("as_voice", True) else TurnRequest(text_input=text)
            async for event in pipeline.stream_turn(request):
                payload = {"kind": event.kind, "data": event.data}
                if event.kind == "done":
                    payload["data"] = event.data["result"].to_dict()
                    _turns_total += 1
                    if event.data["result"].failed_stage:
                        _failures_total += 1
                await ws.send_json(payload)
    except WebSocketDisconnect:
        return


def run() -> None:  # pragma: no cover
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":  # pragma: no cover
    run()

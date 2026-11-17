const $ = (id) => document.getElementById(id);
let budgets = {};

async function loadBudget() {
  const b = await (await fetch("/budget")).json();
  for (const s of b.stages) budgets[s.stage] = s.budget_ms;
}
loadBudget();

function drawWaterfall(timing) {
  const order = ["capture","vad_endpointing","network_ingress","stt","llm_first_token","llm_complete","tts_first_chunk","tts_complete","network_egress"];
  const maxMs = 1600;
  $("waterfall").innerHTML = order.filter(s => timing.stages[s]).map(s => {
    const t = timing.stages[s];
    const pct = Math.min(100, (t.elapsed_ms / maxMs) * 100);
    const tick = Math.min(100, ((budgets[s] || t.budget_ms) / maxMs) * 100);
    const cls = t.timed_out || t.over_budget ? "over" : (t.elapsed_ms > 0.8 * t.budget_ms ? "warn" : "");
    const label = t.skipped ? "skipped" : `${t.elapsed_ms.toFixed(0)} / ${t.budget_ms.toFixed(0)} ms${t.timed_out ? " ⏱ timeout" : ""}`;
    return `<div class="stage"><span>${s}</span><div class="bar"><div class="fill ${cls}" style="width:${pct}%"></div><div class="tick" style="left:${tick}%"></div></div><span>${label}</span></div>`;
  }).join("");
  $("firstAudioBadge").textContent = `first audio: ${timing.first_audio_ms.toFixed(0)} ms`;
  $("firstAudioBadge").style.color = timing.first_audio_ms <= 800 ? "var(--ok)" : "var(--bad)";
}

async function refreshStats() {
  const s = await (await fetch("/stats")).json();
  $("modeBadge").textContent = `mode: ${s.mode}`;
  $("statsOut").textContent = JSON.stringify(s, null, 2);
}

function speak(text) {
  try {
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.05;
    speechSynthesis.cancel();
    speechSynthesis.speak(u);
  } catch (e) { /* browser TTS unavailable — text reply still shown */ }
}

async function sendTurn(text) {
  if (!text.trim()) return;
  $("transcript").textContent = "You: " + text;
  $("reply").textContent = "…streaming";
  const res = await fetch("/turn", { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ text, as_voice: true }) });
  const r = await res.json();
  $("reply").textContent = "Vox: " + r.reply_text + (r.failed_stage ? `  (stage failed: ${r.failed_stage} — degraded to ${r.mode})` : "");
  drawWaterfall(r.timing);
  if (r.mode !== "text_only" && r.mode !== "safe_fail") speak(r.reply_text);
  refreshStats();
}

$("sendBtn").onclick = () => sendTurn($("textIn").value);
$("textIn").addEventListener("keydown", (e) => { if (e.key === "Enter") sendTurn(e.target.value); });

$("micBtn").onclick = () => {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { alert("Speech recognition isn't available in this browser — type instead, the pipeline is identical."); return; }
  const rec = new SR();
  rec.lang = "en-US"; rec.interimResults = false;
  $("micBtn").textContent = "🔴 Listening…";
  rec.onresult = (e) => { const text = e.results[0][0].transcript; $("textIn").value = text; sendTurn(text); };
  rec.onend = () => { $("micBtn").textContent = "🎤 Speak"; };
  rec.onerror = () => { $("micBtn").textContent = "🎤 Speak"; };
  rec.start();
};

document.querySelectorAll("[data-chaos]").forEach(btn => {
  btn.onclick = async () => {
    const kind = btn.dataset.chaos;
    let body = {};
    if (kind === "stt-delay") body = { stage: "stt", extra_delay_ms: 900 };
    if (kind === "llm-fail") body = { stage: "llm_first_token", fail: true };
    if (kind === "tts-fail") body = { stage: "tts_first_chunk", fail: true };
    if (kind === "clear") body = { clear: true };
    const r = await (await fetch("/chaos", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body) })).json();
    $("chaosOut").textContent = "chaos now: " + JSON.stringify(r) + "\nSend a turn to see the timeout / degradation behaviour.";
  };
});
refreshStats();

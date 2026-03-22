import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, render_template_string, request, send_from_directory

app = Flask(__name__)
DB_PATH = "quant_sim.db"
REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")
BRIDGE_SCRIPT = os.path.join(os.path.dirname(__file__), "web_bridge.py")
BRIDGE_PREFIX = "__BRIDGE_JSON__"
chat_history = []
chat_lock = threading.Lock()
notebook_bridge_lock = threading.Lock()
NOTEBOOK_BRIDGE_COMMANDS = {"notebook-status", "setup-auth", "ask-mda", "why-not-buy", "manual-refresh"}


def _db_conn():
    return sqlite3.connect(DB_PATH)


def run_bridge(command: str, payload: dict | None = None, timeout: int = 300) -> dict:
    input_text = json.dumps(payload or {}, ensure_ascii=False)
    def stop_bridge_process(proc: subprocess.Popen | None):
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def parse_bridge_output(combined_output: str) -> dict | None:
        stdout = (combined_output or "").splitlines()
        for line in reversed(stdout):
            if line.startswith(BRIDGE_PREFIX):
                try:
                    return json.loads(line[len(BRIDGE_PREFIX):])
                except json.JSONDecodeError:
                    break
        return None

    def invoke_bridge() -> tuple[int | None, str, dict | None]:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".bridge.log")
        output_path = temp_file.name
        temp_file.close()
        proc = None
        try:
            with open(output_path, "wb") as stream:
                proc = subprocess.Popen(
                    [sys.executable, BRIDGE_SCRIPT, command],
                    stdin=subprocess.PIPE,
                    stdout=stream,
                    stderr=stream,
                    cwd=os.path.dirname(__file__),
                    close_fds=True,
                )
                if proc.stdin:
                    proc.stdin.write(input_text.encode("utf-8"))
                    proc.stdin.close()
                deadline = time.monotonic() + timeout
                combined_output = ""
                while time.monotonic() < deadline:
                    with open(output_path, "r", encoding="utf-8", errors="replace") as reader:
                        combined_output = reader.read()
                    payload = parse_bridge_output(combined_output)
                    if payload is not None:
                        stop_bridge_process(proc)
                        return proc.returncode, combined_output, payload
                    if proc.poll() is not None:
                        break
                    time.sleep(0.5)
            with open(output_path, "r", encoding="utf-8", errors="replace") as stream:
                combined_output = stream.read()
            return proc.returncode, combined_output, parse_bridge_output(combined_output)
        finally:
            stop_bridge_process(proc)
            try:
                os.remove(output_path)
            except OSError:
                pass

    if command in NOTEBOOK_BRIDGE_COMMANDS:
        with notebook_bridge_lock:
            return_code, combined_output, payload = invoke_bridge()
    else:
        return_code, combined_output, payload = invoke_bridge()

    if payload is not None:
        return payload

    stdout = (combined_output or "").splitlines()
    return {
        "success": False,
        "error": f"桥接进程未返回可解析 JSON（exit={return_code}）",
        "stdout_tail": "\n".join(stdout[-20:]),
    }


def load_status():
    with _db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT initial_capital, balance, total_assets, total_pnl, updated_at FROM account WHERE id = 1")
        row = cur.fetchone()
        account = None
        if row:
            account = {
                "initial_capital": row[0],
                "balance": row[1],
                "total_assets": row[2],
                "total_pnl": row[3],
                "updated_at": row[4],
                "return_pct": ((row[2] / row[0] - 1) * 100) if row[0] else 0.0,
            }

        cur.execute(
            "SELECT symbol, name, quantity, avg_price, current_price, bought_at, target_price, stop_loss_price, trailing_active, high_water_price, partial_exit_done "
            "FROM positions ORDER BY bought_at DESC"
        )
        positions = [
            {
                "symbol": r[0],
                "name": r[1],
                "quantity": r[2],
                "avg_price": r[3],
                "current_price": r[4],
                "bought_at": r[5],
                "target_price": r[6],
                "stop_loss_price": r[7],
                "trailing_active": bool(r[8]),
                "high_water_price": r[9],
                "partial_exit_done": bool(r[10]),
            }
            for r in cur.fetchall()
        ]
        for position in positions:
            bought_at = datetime.fromisoformat(position["bought_at"])
            unlock_at = bought_at.timestamp() + 30 * 60
            remaining_seconds = max(0, unlock_at - datetime.now().timestamp())
            position["lock_status"] = f"锁定中({int((remaining_seconds + 59) // 60)}min)" if remaining_seconds > 0 else "已解锁"

        cur.execute(
            "SELECT timestamp, symbol, name, action, price, quantity, fee, reason "
            "FROM trades ORDER BY timestamp DESC LIMIT 20"
        )
        trades = [
            {
                "timestamp": r[0],
                "symbol": r[1],
                "name": r[2],
                "action": r[3],
                "price": r[4],
                "quantity": r[5],
                "fee": r[6],
                "reason": r[7],
            }
            for r in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT timestamp, action, symbol, name, total_score, win_rate_confidence,
                   data_arch_score, notebook_score, game_score, trend_score,
                   risk_text, lock_status, thinking_trace, logic, reason
            FROM mda_snapshots
            ORDER BY timestamp DESC
            LIMIT 1
            """
        )
        snapshot_row = cur.fetchone()
        snapshot = None
        if snapshot_row:
            import json
            snapshot = {
                "timestamp": snapshot_row[0],
                "action": snapshot_row[1],
                "symbol": snapshot_row[2],
                "name": snapshot_row[3],
                "total_score": snapshot_row[4],
                "win_rate_confidence": snapshot_row[5],
                "dimension_scores": {
                    "data_arch": snapshot_row[6],
                    "notebooklm": snapshot_row[7],
                    "game_psych": snapshot_row[8],
                    "trend": snapshot_row[9],
                },
                "risk_text": snapshot_row[10],
                "lock_status": snapshot_row[11],
                "thinking_trace": json.loads(snapshot_row[12]) if snapshot_row[12] else {},
                "logic": json.loads(snapshot_row[13]) if snapshot_row[13] else {},
                "reason": snapshot_row[14],
            }

    return {
        "account": account,
        "positions": positions,
        "trades": trades,
        "snapshot": snapshot,
        "generated_at": datetime.now().isoformat(),
    }

@app.get("/")
def index():
    return render_template_string(PAGE_HTML)


@app.get("/api/status")
def api_status():
    return jsonify(load_status())


@app.get("/api/chat/history")
def api_chat_history():
    with chat_lock:
        return jsonify({"messages": chat_history[-50:]})


@app.post("/api/chat")
def api_chat():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message 不能为空"}), 400

    with chat_lock:
        local_history = [m for m in chat_history if m.get("role") == "assistant" or m.get("role") == "user"]
        paired = []
        q = None
        for m in local_history:
            if m["role"] == "user":
                q = m["content"]
            elif m["role"] == "assistant" and q:
                paired.append({"q": q, "a": m["content"]})
                q = None

    if message.lower() in {"check", "刷新"}:
        snapshot = run_bridge("manual-refresh", timeout=360)
        answer = (
            f"已执行完整 MDA 决策流程。当前动作：{snapshot.get('action', '观望')}；"
            f"综合胜率 {snapshot.get('total_score', 0):.2f}% 。"
            f"操作逻辑：{snapshot.get('reason') or '等待下一轮信号。'}"
        )
        used_notebooklm = snapshot.get("knowledge_source") == "notebooklm"
        kb_preview = ""
        kb_summary = ""
        kb_status = snapshot.get("knowledge_source", "n/a")
        kb_error = snapshot.get("error")
    else:
        why_not_match = re.match(r"^为什么不买入\s+(.+)$", message)
        if why_not_match:
            symbol = why_not_match.group(1).strip()
            result = run_bridge(
                "why-not-buy",
                {"symbol": symbol, "history": paired},
                timeout=300,
            )
            breakdown = result.get("dimension_breakdown") or []
            lines = [f"{item.get('name')}: {item.get('score', 0)}/{item.get('max_score', 25)}" for item in breakdown]
            for item in breakdown:
                for deduction in item.get("deductions", []):
                    lines.append(f"- {deduction}")
            answer = (
                f"{symbol} 当前不满足买入阈值。\n"
                f"综合评分: {result.get('final_score', 0)}/{result.get('pass_threshold', 75)}\n"
                f"{chr(10).join(lines)}\n"
                f"结论: {result.get('conclusion', '暂无更详细解释。')}"
            )
            used_notebooklm = result.get("knowledge_status") == "ok"
            kb_preview = answer
            kb_summary = result.get("conclusion", "")
            kb_status = result.get("knowledge_status", "unavailable")
            kb_error = result.get("error")
        else:
            result = run_bridge(
                "ask-mda",
                {"message": message, "history": paired},
                timeout=300,
            )
            answer = (result or {}).get("final_answer") or "未生成有效回答，请稍后重试。"
            used_notebooklm = bool((result or {}).get("used_notebooklm"))
            kb_preview = (result or {}).get("kb_preview") or ""
            kb_structured = (result or {}).get("kb_structured") or {}
            kb_summary = kb_structured.get("kb_summary") or ""
            kb_status = (result or {}).get("kb_status", "unavailable")
            kb_error = (result or {}).get("kb_error")
    if message.lower() in {"check", "刷新"}:
        kb_status = "n/a"
        kb_error = None

    with chat_lock:
        chat_history.append({"role": "user", "content": message, "timestamp": datetime.now().isoformat()})
        chat_history.append({"role": "assistant", "content": answer, "timestamp": datetime.now().isoformat()})

    return jsonify(
        {
            "answer": answer,
            "used_notebooklm": used_notebooklm,
            "kb_summary": kb_summary,
            "kb_preview": kb_preview,
            "kb_status": kb_status,
            "kb_error": kb_error,
        }
    )


@app.post("/api/mda/refresh")
def api_mda_refresh():
    snapshot = run_bridge("manual-refresh", timeout=360)
    return jsonify({
        "ok": not bool(snapshot.get("error")),
        "snapshot": snapshot,
        "generated_at": datetime.now().isoformat(),
    })


@app.post("/api/notebooklm/setup-auth")
def api_notebooklm_setup_auth():
    result = run_bridge("setup-auth", timeout=300)
    return jsonify(result)


@app.get("/api/notebooklm/status")
def api_notebooklm_status():
    result = run_bridge("notebook-status", timeout=300)
    return jsonify(result)


@app.get("/reports/<path:filename>")
def report_files(filename):
    return send_from_directory(REPORTS_DIR, filename)


PAGE_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>多域预判 Web 控制台</title>
  <style>
    body { margin: 0; font-family: "Segoe UI", sans-serif; background: #0b1220; color: #e5e7eb; }
    .layout { display: grid; grid-template-columns: 460px 1fr; min-height: 100vh; }
    .left { border-right: 1px solid #1f2937; padding: 16px; display: flex; flex-direction: column; gap: 12px; }
    .right { padding: 16px; display: grid; grid-template-rows: auto auto 1fr; gap: 12px; }
    .panel { background: #111827; border: 1px solid #1f2937; border-radius: 10px; padding: 12px; }
    .title { font-weight: 700; margin-bottom: 8px; }
    #chatBox { height: 58vh; overflow: auto; padding: 8px; background: #0f172a; border-radius: 8px; border: 1px solid #1f2937; }
    .msg { margin: 8px 0; line-height: 1.5; white-space: pre-wrap; }
    .user { color: #93c5fd; }
    .assistant { color: #c7d2fe; }
    .sys { color: #86efac; }
    .row { display: flex; gap: 8px; }
    input, button { border-radius: 8px; border: 1px solid #374151; background: #0b1220; color: #e5e7eb; padding: 8px; }
    input { flex: 1; }
    button { cursor: pointer; }
    .cards { display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 8px; }
    .card { background: #0f172a; border: 1px solid #1f2937; border-radius: 8px; padding: 10px; }
    .k { font-size: 12px; color: #94a3b8; }
    .v { font-size: 20px; margin-top: 6px; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th, td { border-bottom: 1px solid #1f2937; padding: 6px; text-align: left; }
    iframe { width: 100%; height: 380px; border: 1px solid #1f2937; border-radius: 8px; background: #fff; }
    .mda { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }
    .pill { display: inline-block; padding: 4px 8px; border-radius: 999px; border: 1px solid #334155; font-size: 12px; color: #93c5fd; }
  </style>
</head>
<body>
  <div class="layout">
    <div class="left">
      <div class="panel">
        <div class="title">多域预判连续问答</div>
        <div id="chatBox"></div>
        <div class="row" style="margin-top:8px;">
          <input id="question" placeholder="输入问题并回车..." />
          <button id="sendBtn">发送</button>
          <button id="refreshBtn">Check/刷新</button>
          <button id="authBtn">知识库登录</button>
        </div>
        <div style="margin-top:8px; font-size:12px; color:#94a3b8;">
          支持 `Check` / `刷新` 执行完整 MDA 决策，也支持 `为什么不买入 sh600519` 拆解四维扣分。
        </div>
      </div>
    </div>
    <div class="right">
      <div class="panel">
        <div class="title">资金概览</div>
        <div class="cards">
          <div class="card"><div class="k">总资产</div><div class="v" id="totalAssets">-</div></div>
          <div class="card"><div class="k">可用资金</div><div class="v" id="balance">-</div></div>
          <div class="card"><div class="k">累计盈亏</div><div class="v" id="pnl">-</div></div>
          <div class="card"><div class="k">收益率</div><div class="v" id="ret">-</div></div>
        </div>
      </div>
      <div class="panel">
        <div class="title">最新 MDA 快照</div>
        <div class="mda">
          <div class="card"><div class="k">综合评分</div><div class="v" id="mdaScore">-</div></div>
          <div class="card"><div class="k">最终动作</div><div class="v" id="mdaAction">-</div></div>
          <div class="card"><div class="k">锁仓状态</div><div class="v" id="mdaLock">-</div></div>
        </div>
        <div style="margin-top:10px;">
          <span class="pill" id="scoreData">数据架构 -</span>
          <span class="pill" id="scoreKb">知识库 -</span>
          <span class="pill" id="scoreGame">博弈心理 -</span>
          <span class="pill" id="scoreTrend">趋势动能 -</span>
        </div>
        <div style="margin-top:10px; line-height:1.6; color:#cbd5e1;" id="mdaReason">-</div>
      </div>
      <div class="panel">
        <div class="title">持仓与交易（自动刷新）</div>
        <div id="tables"></div>
      </div>
      <div class="panel">
        <div class="title">模拟详情页（内嵌 dashboard）</div>
        <iframe src="/reports/dashboard.html"></iframe>
      </div>
    </div>
  </div>

<script>
const chatBox = document.getElementById("chatBox");
const questionInput = document.getElementById("question");
const sendBtn = document.getElementById("sendBtn");
const refreshBtn = document.getElementById("refreshBtn");
const authBtn = document.getElementById("authBtn");

function addMsg(role, text) {
  const div = document.createElement("div");
  div.className = "msg " + role;
  const who = role === "user" ? "你" : (role === "assistant" ? "multi-domain-foresight" : "系统");
  div.textContent = `${who}: ${text}`;
  chatBox.appendChild(div);
  chatBox.scrollTop = chatBox.scrollHeight;
}

function addTrace(text) {
  const div = document.createElement("div");
  div.className = "msg sys";
  div.textContent = `知识库状态: ${text}`;
  chatBox.appendChild(div);
  chatBox.scrollTop = chatBox.scrollHeight;
}

async function sendQuestion() {
  const msg = questionInput.value.trim();
  if (!msg) return;
  addMsg("user", msg);
  questionInput.value = "";
  const r = await fetch("/api/chat", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({message: msg})
  });
  const d = await r.json();
  addMsg("assistant", d.answer || "无响应");
  if (d.kb_status === "ok") {
    const trace = d.kb_summary || d.kb_preview || "NotebookLM 已返回内容";
    addTrace(trace.slice(0, 220));
  } else if (d.kb_status === "local_rag") {
    const trace = d.kb_summary || d.kb_preview || "已回退到本地知识库";
    addTrace(`NotebookLM 不可用，已改用本地知识库。${trace.slice(0, 220)}`);
  } else if (d.kb_status === "n/a") {
    addTrace("本条消息未调用知识库。");
  } else {
    addTrace(`本轮未拿到 NotebookLM 回执。${d.kb_error || "请检查 NotebookLM 登录状态或浏览器启动。"} `);
  }
}

async function refreshStatus() {
  const r = await fetch("/api/status");
  const d = await r.json();
  const a = d.account || {};
  const s = d.snapshot || {};
  document.getElementById("totalAssets").textContent = a.total_assets?.toFixed?.(2) ?? "-";
  document.getElementById("balance").textContent = a.balance?.toFixed?.(2) ?? "-";
  document.getElementById("pnl").textContent = a.total_pnl?.toFixed?.(2) ?? "-";
  document.getElementById("ret").textContent = (a.return_pct !== undefined) ? `${a.return_pct.toFixed(2)}%` : "-";
  document.getElementById("mdaScore").textContent = (s.total_score !== undefined) ? `${Number(s.total_score).toFixed(2)}%` : "-";
  document.getElementById("mdaAction").textContent = s.action || "-";
  document.getElementById("mdaLock").textContent = s.lock_status || "-";
  document.getElementById("mdaReason").textContent = s.reason || "暂无最新决策说明";
  document.getElementById("scoreData").textContent = `数据架构 ${Number(s.dimension_scores?.data_arch ?? 0).toFixed(1)}`;
  document.getElementById("scoreKb").textContent = `知识库 ${Number(s.dimension_scores?.notebooklm ?? 0).toFixed(1)}`;
  document.getElementById("scoreGame").textContent = `博弈心理 ${Number(s.dimension_scores?.game_psych ?? 0).toFixed(1)}`;
  document.getElementById("scoreTrend").textContent = `趋势动能 ${Number(s.dimension_scores?.trend ?? 0).toFixed(1)}`;

  const posRows = (d.positions || []).map(p => `<tr>
    <td>${p.symbol}</td><td>${p.name}</td><td>${p.quantity}</td><td>${p.avg_price}</td><td>${p.current_price}</td><td>${p.lock_status || "-"}</td>
  </tr>`).join("");
  const tradeRows = (d.trades || []).slice(0, 8).map(t => `<tr>
    <td>${t.timestamp || ""}</td><td>${t.action}</td><td>${t.symbol}</td><td>${t.price}</td><td>${t.quantity}</td><td>${t.reason || ""}</td>
  </tr>`).join("");

  document.getElementById("tables").innerHTML = `
    <h4>持仓</h4>
    <table><thead><tr><th>代码</th><th>名称</th><th>数量</th><th>成本</th><th>现价</th><th>锁仓</th></tr></thead><tbody>${posRows || "<tr><td colspan='6'>无</td></tr>"}</tbody></table>
    <h4>最近交易</h4>
    <table><thead><tr><th>时间</th><th>动作</th><th>代码</th><th>价格</th><th>数量</th><th>理由</th></tr></thead><tbody>${tradeRows || "<tr><td colspan='6'>无</td></tr>"}</tbody></table>
  `;
}

async function manualRefresh() {
  addMsg("sys", "正在执行完整 MDA 决策流程...");
  const r = await fetch("/api/mda/refresh", { method: "POST" });
  const d = await r.json();
  if (d.snapshot) {
    addMsg("assistant", `刷新完成：动作 ${d.snapshot.action || "观望"}，综合评分 ${(Number(d.snapshot.total_score || 0)).toFixed(2)}%，原因：${d.snapshot.reason || "暂无"}`);
  }
  refreshStatus();
}

async function setupNotebookAuth() {
  addMsg("sys", "正在检查 NotebookLM 登录状态，必要时会启动登录流程...");
  const r = await fetch("/api/notebooklm/setup-auth", { method: "POST" });
  const d = await r.json();
  if (d.success) {
    addTrace(d.message || "NotebookLM 登录流程已启动或认证成功。");
  } else {
    addTrace(`NotebookLM 登录未完成：${d.error || "未知错误"}`);
  }
  await refreshStatus();
}

sendBtn.onclick = sendQuestion;
refreshBtn.onclick = manualRefresh;
authBtn.onclick = setupNotebookAuth;
questionInput.addEventListener("keydown", (e) => { if (e.key === "Enter") sendQuestion(); });

addMsg("sys", "Web 控制台已连接。你可以持续提问，我会用 multi-domain-foresight 方式回答。");
refreshStatus();
setInterval(refreshStatus, 10000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    # threaded=True 便于聊天与状态刷新并发
    app.run(host="127.0.0.1", port=7860, debug=False, threaded=True)

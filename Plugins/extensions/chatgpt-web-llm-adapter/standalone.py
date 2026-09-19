from __future__ import annotations

import argparse
import html
import json
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, Request, build_opener

# Standalone mode intentionally uses its own fixed loopback pair so it can
# coexist with the optional FolderBridge Extension service (8767/8768).
os.environ.setdefault("CHATGPT_WEB_ADAPTER_PORT", "8769")
os.environ.setdefault("CHATGPT_WEB_ADAPTER_DEVTOOLS_PORT", "8770")

from browser_runtime import (  # noqa: E402
    ADAPTER_ID,
    BASE_URL,
    DEVTOOLS_PORT,
    HOST,
    MODEL_ID,
    PORT,
    ExtensionError,
    _atomic_json,
    _read_json,
    connection_info,
    connection_path,
    health,
    run_service,
)

STANDALONE_VERSION = "0.3.2"
STATE_FOLDER = "ChatGPT-Web-LLM-Test-Adapter"
STATE_VERSION = "standalone-v1"


def default_state_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / STATE_FOLDER / STATE_VERSION
    return Path.home() / ".chatgpt-web-llm-test-adapter" / STATE_VERSION


def cancel_path(state_dir: Path) -> Path:
    return state_dir / "stop.requested"


def status_page_path(state_dir: Path) -> Path:
    return state_dir / "status.html"


def _opener():
    return build_opener(ProxyHandler({}))


def _connection_record(state_dir: Path) -> dict[str, Any]:
    return _read_json(connection_path(state_dir)) or {}


def _status_html(record: dict[str, Any]) -> str:
    token = str(record.get("api_key") or "")
    base_url = str(record.get("base_url") or BASE_URL)
    model = str(record.get("model") or MODEL_ID)
    root_url = base_url.removesuffix("/v1")
    health_url = root_url + "/health"
    control_url = root_url + "/control/open-login"
    parallel_url = root_url + "/control/max-parallel"
    max_parallel = max(1, min(4, int(record.get("max_parallel") or 2)))
    escaped = {key: html.escape(value, quote=True) for key, value in {"token": token, "base": base_url, "model": model, "health": health_url}.items()}
    js_health = json.dumps(health_url)
    js_control = json.dumps(control_url)
    js_parallel = json.dumps(parallel_url)
    js_token = json.dumps(token)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ChatGPT Web LLM Adapter · Standalone {STANDALONE_VERSION}</title>
<style>
:root{{font-family:"Segoe UI","Microsoft YaHei",sans-serif;color-scheme:dark;background:#080b10;color:#e8eef7}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at 50% 0,#162238,#080b10 55%)}}main{{width:min(780px,calc(100vw - 32px));background:#101722;border:1px solid #2b3e57;border-radius:14px;padding:26px;box-shadow:0 22px 70px #0008}}h1{{margin:0 0 8px;font-size:24px}}.sub{{color:#91a3bb;margin-bottom:22px}}.state{{display:inline-flex;align-items:center;gap:8px;border:1px solid #44536a;border-radius:999px;padding:7px 12px;margin-bottom:18px}}.dot{{width:9px;height:9px;border-radius:50%;background:#d4a646}}.state.ready .dot{{background:#4bd383;box-shadow:0 0 14px #4bd383}}.state.error .dot{{background:#ef6b72}}.grid{{display:grid;gap:12px}}label{{display:block;color:#91a3bb;font-size:12px;margin-bottom:5px}}.row{{display:flex;gap:8px}}code,input{{font:13px Consolas,monospace}}input{{flex:1;min-width:0;background:#080d15;border:1px solid #31455f;color:#e8eef7;border-radius:7px;padding:11px}}input[type=range]{{padding:0;accent-color:#6ea8ff}}.parallel-card{{margin-top:6px;padding:14px;border:1px solid #2b3e57;border-radius:9px;background:#0b111a}}.parallel-head{{display:flex;justify-content:space-between;gap:12px;align-items:center}}.parallel-head b{{color:#9ec3ff}}.parallel-meta{{margin-top:7px;color:#91a3bb;font-size:12px}}.parallel-row{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:center;margin-top:10px}}.parallel-row input{{width:100%}}.safety-card{{margin-top:10px;padding:14px;border:1px solid #31455f;border-radius:9px;background:#0b111a}}.safety-card b{{color:#9ec3ff}}.safety-state{{margin-top:8px;color:#c7d8ef;font:12px/1.7 Consolas,monospace}}button{{border:1px solid #416aa0;background:#17345d;color:#dceaff;border-radius:7px;padding:9px 13px;cursor:pointer}}.note{{margin-top:20px;color:#91a3bb;line-height:1.7;font-size:12px;border-top:1px solid #25354a;padding-top:16px}}.warn{{color:#d8b66e}}@media(max-width:620px){{.row{{flex-direction:column}}.parallel-row{{grid-template-columns:1fr}}}}
</style></head><body><main>
<h1>ChatGPT Web LLM Test Adapter</h1><div class="sub">Standalone {STANDALONE_VERSION} · 不依赖 ChatGPT→FolderBridge 控制面</div>
<div id="state" class="state"><span class="dot"></span><b id="stateText">CHECKING</b></div>
<div class="row" style="margin:0 0 18px"><button id="loginBtn" onclick="openLoginPage()">打开 ChatGPT 登录 / 验证页</button><span id="loginMsg" style="align-self:center;color:#91a3bb;font-size:12px"></span></div>
<div class="grid">
<div><label>Base URL</label><div class="row"><input id="base" readonly value="{escaped['base']}"><button onclick="copyField('base')">复制</button></div></div>
<div><label>Model</label><div class="row"><input id="model" readonly value="{escaped['model']}"><button onclick="copyField('model')">复制</button></div></div>
<div><label>本次临时 API Key</label><div class="row"><input id="token" readonly value="{escaped['token']}"><button onclick="copyField('token')">复制</button></div></div>
<div class="parallel-card"><div class="parallel-head"><span>网页 ChatGPT 并发上限</span><b><span id="parallelValue">{max_parallel}</span> 路</b></div><div class="parallel-row"><input id="parallel" type="range" min="1" max="4" step="1" value="{max_parallel}" aria-label="网页 ChatGPT 并发上限"><span id="parallelState">等待状态…</span></div><div id="parallelMsg" class="parallel-meta">并发只限制同时工作的网页会话；0.3.2 另外限制 fresh chat 的启动频率与滚动窗口。</div></div>
<div class="safety-card"><b>历史安全节流</b><div class="parallel-meta">至少 20 秒启动间隔 · 5 分钟最多 8 个 fresh chat · 命中历史限流后 2/4/8/10 分钟指数冷却 · 登录/验证优先复用已有 ChatGPT 页。</div><div id="safetyState" class="safety-state">等待状态…</div><div class="parallel-meta warn">当前仍是普通 fresh chat；Temporary Chat 尚未强制启用，因此节流不能等价为“不写入聊天历史”。</div></div>
</div>
<div class="note"><b>用途：</b>《认知星图》、Debate-Judge 或其它本地 OpenAI-compatible 客户端的网页 ChatGPT 语义验收。每个 completion 都使用 fresh ChatGPT conversation。<br><span class="warn">不是 OpenAI 官方 API 等价物；system role 使用 prompt_wrapped 语义。</span><br>日常启动 / 重启 / 停止优先使用 FolderBridge 的 Extensions &amp; Skills 托管控制；项目根目录 CMD 仅保留为 recovery / fallback。</div>
</main><script>
function copyField(id){{const el=document.getElementById(id);el.select();el.setSelectionRange(0,99999);if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(el.value);else document.execCommand('copy');}}
async function openLoginPage(){{const btn=document.getElementById('loginBtn'),msg=document.getElementById('loginMsg');btn.disabled=true;msg.textContent='正在打开专用浏览器…';try{{const r=await fetch({js_control},{{method:'POST',headers:{{'Authorization':'Bearer '+{js_token}}}}});const x=await r.json();if(!r.ok)throw new Error(x?.error?.message||('HTTP '+r.status));msg.textContent=x.opened?'已在 Standalone 专用 Chrome 打开。':'未能打开。';}}catch(e){{msg.textContent='打开失败：'+String(e.message||e);}}finally{{btn.disabled=false;}}}}
async function setParallel(value){{const slider=document.getElementById('parallel'),msg=document.getElementById('parallelMsg');slider.disabled=true;msg.textContent='正在更新并发上限…';try{{const r=await fetch({js_parallel},{{method:'POST',headers:{{'Authorization':'Bearer '+{js_token},'Content-Type':'application/json'}},body:JSON.stringify({{max_parallel:Number(value)}})}});const x=await r.json();if(!r.ok)throw new Error(x?.error?.message||('HTTP '+r.status));slider.value=String(x.max_parallel);document.getElementById('parallelValue').textContent=String(x.max_parallel);msg.textContent='已生效：最多 '+x.max_parallel+' 路网页会话同时工作；其余请求排队。';}}catch(e){{msg.textContent='调整失败：'+String(e.message||e);}}finally{{slider.disabled=false;}}}}
const parallel=document.getElementById('parallel');parallel.addEventListener('input',()=>{{document.getElementById('parallelValue').textContent=parallel.value;}});parallel.addEventListener('change',()=>setParallel(parallel.value));
async function poll(){{const box=document.getElementById('state'),text=document.getElementById('stateText'),slider=document.getElementById('parallel'),parallelState=document.getElementById('parallelState'),safety=document.getElementById('safetyState');try{{const r=await fetch({js_health},{{cache:'no-store'}});const x=await r.json();if(x.ready){{box.className='state ready';text.textContent='READY';}}else{{box.className='state';const s=x.provider_state||'waiting_login_or_challenge';text.textContent=s==='history_rate_limited'?'HISTORY_COOLDOWN':s==='page_loading'?'WAITING_PAGE':s==='chatgpt_page_missing'?'WAITING_CHATGPT_PAGE':s==='devtools_unavailable'?'WAITING_BROWSER':'WAITING_LOGIN_OR_CHALLENGE';}}if(Number.isInteger(x.max_parallel)){{if(document.activeElement!==slider&&!slider.disabled){{slider.value=String(x.max_parallel);document.getElementById('parallelValue').textContent=String(x.max_parallel);}}parallelState.textContent=String(x.active_requests||0)+' / '+x.max_parallel+' 活跃';}}safety.textContent='窗口 '+String(x.fresh_chat_starts_in_window??0)+' / '+String(x.fresh_chat_window_max??8)+' · 下一次最早 '+String(x.fresh_chat_next_start_in_seconds??0)+'s · 冷却剩余 '+String(x.history_cooldown_remaining_seconds??0)+'s · 限流命中 '+String(x.history_rate_limit_strikes??0)+' 次';}}catch(e){{box.className='state error';text.textContent='OFFLINE';parallelState.textContent='状态不可用';safety.textContent='状态不可用';}}}}
poll();setInterval(poll,1500);
</script></body></html>"""


def write_status_page(state_dir: Path, record: dict[str, Any]) -> Path:
    path = status_page_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(_status_html(record), encoding="utf-8")
    os.replace(temporary, path)
    return path


def open_status_page(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            webbrowser.open(path.resolve().as_uri())
    except Exception:
        pass


def print_connection(record: dict[str, Any]) -> None:
    print(f"\n=== ChatGPT Web LLM Adapter · Standalone {STANDALONE_VERSION} ===", flush=True)
    print(f"Base URL : {record.get('base_url', BASE_URL)}", flush=True)
    print(f"Model    : {record.get('model', MODEL_ID)}", flush=True)
    print(f"API Key  : {record.get('api_key', '')}", flush=True)
    print(f"API port : {PORT} | DevTools port: {DEVTOOLS_PORT}", flush=True)
    print("每个请求都会创建 fresh ChatGPT conversation；0.3.2 会统一节流 fresh chat 启动。", flush=True)
    print("首次使用请在专用 Chrome/Edge 窗口完成 ChatGPT 登录。", flush=True)
    print("本服务仅用于本机语义验收，不等价于 OpenAI 官方 API。\n", flush=True)


def announce_when_started(state_dir: Path, *, open_page: bool) -> None:
    for _ in range(240):
        record = _connection_record(state_dir)
        if record.get("active") and isinstance(record.get("api_key"), str) and record.get("api_key"):
            page = write_status_page(state_dir, record)
            print_connection(record)
            print(f"状态页  : {page}", flush=True)
            if open_page:
                open_status_page(page)
            return
        time.sleep(0.25)


def request_stop(state_dir: Path) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = cancel_path(state_dir)
    marker.write_text("stop\n", encoding="utf-8")
    if not health():
        print("Standalone Adapter 当前未运行。")
        return 0
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not health():
            print("Standalone Adapter 已停止。")
            return 0
        time.sleep(0.25)
    print("已发送停止请求，但服务仍在响应；请关闭其控制台窗口。", file=sys.stderr)
    return 2


def print_status(state_dir: Path) -> int:
    info = connection_info(state_dir, include_token=False)
    info["standalone_version"] = STANDALONE_VERSION
    info["state_dir"] = str(state_dir)
    info["status_page"] = str(status_page_path(state_dir))
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if info.get("running") else 1


def live_probe(state_dir: Path) -> int:
    record = _connection_record(state_dir)
    live = health(timeout=3.0)
    if not live:
        print("LIVE_PROBE_FAIL: standalone service is offline", file=sys.stderr)
        return 2
    if not live.get("ready"):
        state = str(live.get("provider_state") or "waiting_login_or_challenge")
        print(f"LIVE_PROBE_WAITING_PROVIDER: {state}; make the dedicated ChatGPT page ready first", file=sys.stderr)
        return 3
    token = record.get("api_key")
    if not record.get("active") or not isinstance(token, str) or not token:
        print("LIVE_PROBE_FAIL: active standalone connection token is unavailable", file=sys.stderr)
        return 4
    body = json.dumps({
        "model": record.get("model") or MODEL_ID,
        "messages": [
            {"role": "system", "content": "Return exactly one JSON object and nothing else."},
            {"role": "user", "content": "Return exactly {\"ok\":true,\"adapter_probe\":\"pass\"}."},
        ],
        "stream": False,
        "response_format": {"type": "json_object"},
    }, ensure_ascii=False).encode("utf-8")
    request = Request(
        str(record.get("base_url") or BASE_URL).rstrip("/") + "/chat/completions",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token, "Origin": "null"},
    )
    try:
        with _opener().open(request, timeout=180) as response:
            envelope = json.loads(response.read().decode("utf-8"))
        content = envelope["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if parsed != {"ok": True, "adapter_probe": "pass"}:
            raise ValueError("unexpected semantic probe payload")
    except Exception as exc:
        print(f"LIVE_PROBE_FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5
    print("LIVE_PROBE_PASS")
    print(json.dumps({
        "base_url": record.get("base_url"),
        "model": record.get("model"),
        "system_semantics": "prompt_wrapped",
        "fresh_chat_per_request": True,
        "response": parsed,
    }, ensure_ascii=False, indent=2))
    return 0


def start_service(state_dir: Path, *, max_parallel: int, response_timeout_seconds: int, open_page: bool) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = cancel_path(state_dir)
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    existing = health()
    if existing:
        record = _connection_record(state_dir)
        if record.get("active") and isinstance(record.get("api_key"), str):
            page = write_status_page(state_dir, record)
            print("Standalone Adapter 已在运行。", flush=True)
            print_connection(record)
            if open_page:
                open_status_page(page)
            return 0
        print(f"端口 {PORT} 已有 ChatGPT Web Adapter 服务，但它不属于当前 standalone state。请停止冲突服务后重试。", file=sys.stderr)
        return 6
    announcer = threading.Thread(target=announce_when_started, args=(state_dir,), kwargs={"open_page": open_page}, daemon=True)
    announcer.start()
    try:
        run_service(state_dir, str(marker), max_parallel=max_parallel, response_timeout_seconds=response_timeout_seconds)
        return 0
    except KeyboardInterrupt:
        marker.write_text("stop\n", encoding="utf-8")
        return 0
    except ExtensionError as exc:
        print(f"START_FAIL [{exc.code}]: {exc}", file=sys.stderr)
        return 7
    except Exception as exc:
        print(f"START_FAIL [{type(exc).__name__}]: {exc}", file=sys.stderr)
        return 8
    finally:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone ChatGPT Web OpenAI-compatible semantic test adapter")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--probe", action="store_true", help="run one real JSON completion against the active standalone service")
    mode.add_argument("--stop", action="store_true", help="request graceful standalone service shutdown")
    mode.add_argument("--status", action="store_true", help="print non-secret standalone health/status")
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--max-parallel", type=int, default=2)
    parser.add_argument("--response-timeout-seconds", type=int, default=600)
    parser.add_argument("--no-status-page", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_dir = args.state_dir.expanduser().resolve()
    if args.probe:
        return live_probe(state_dir)
    if args.stop:
        return request_stop(state_dir)
    if args.status:
        return print_status(state_dir)
    if not 1 <= args.max_parallel <= 4:
        print("--max-parallel 必须是 1–4。", file=sys.stderr)
        return 9
    if not 30 <= args.response_timeout_seconds <= 1800:
        print("--response-timeout-seconds 必须是 30–1800。", file=sys.stderr)
        return 9
    return start_service(
        state_dir,
        max_parallel=args.max_parallel,
        response_timeout_seconds=args.response_timeout_seconds,
        open_page=not args.no_status_page,
    )


if __name__ == "__main__":
    raise SystemExit(main())

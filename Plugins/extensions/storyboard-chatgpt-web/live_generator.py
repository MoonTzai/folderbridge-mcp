from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from folderbridge_mcp.extension_api import ExtensionError
import live_judge


HEX64 = re.compile(r"^[0-9a-f]{64}$")
FILE_ID = re.compile(r"^file_[A-Za-z0-9_-]+$")
EXACT_RECOVERY_POLL_SECONDS = 20.0


def _error(code: str, message: str, *, retryable: bool = False, **details: Any) -> ExtensionError:
    return ExtensionError(code, message, details=details or None, retryable=retryable)


def _binding_path(run_dir: Path) -> Path:
    return run_dir / "provider-bindings.json"


def _conversation_id(url: str) -> str | None:
    return live_judge._conversation_id(url)


def _wait_stable_url(session: live_judge.CdpSession, timeout_seconds: float = 120.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = live_judge._rows(session)
        url = str(state.get("url") or "")
        if _conversation_id(url):
            return url
        time.sleep(0.5)
    raise _error("WAITING_PROVIDER", "Generator conversation did not obtain a stable ChatGPT URL", retryable=True)


def _assistant_count(session: live_judge.CdpSession) -> int:
    value = session.evaluate("""(() => document.querySelectorAll('[data-message-author-role="assistant"]').length)()""")
    return int(value or 0)


def _latest_assistant_text(session: live_judge.CdpSession) -> str:
    value = session.evaluate("""(() => {
      const rows=[...document.querySelectorAll('[data-message-author-role="assistant"]')];
      const el=rows.length?rows[rows.length-1]:null;
      return el ? (el.innerText||el.textContent||'').trim() : '';
    })()""")
    return str(value or "")


def _wait_assistant_after(
    session: live_judge.CdpSession,
    state_dir: Path,
    before: int,
    timeout_seconds: int = 120,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    last = ""
    stable = 0
    while time.monotonic() < deadline:
        if live_judge._history_limited(session):
            live_judge._raise_history_limit(state_dir)
        count = _assistant_count(session)
        text = _latest_assistant_text(session)
        state = live_judge._rows(session)
        if count > before and text and not state.get("stopping"):
            if text == last:
                stable += 1
            else:
                last = text
                stable = 0
            if stable >= 2:
                return text
        time.sleep(1.0)
    raise _error("WAITING_PROVIDER", "Generator role acknowledgement timed out", retryable=True)


def _image_sources(session: live_judge.CdpSession) -> list[dict[str, Any]]:
    value = session.evaluate("""(() => [...document.querySelectorAll('[data-message-author-role="assistant"] img')]
      .map(img=>({src:img.currentSrc||img.src||'',width:img.naturalWidth||0,height:img.naturalHeight||0}))
      .filter(x=>x.src&&x.width>=256&&x.height>=256))()""")
    return value if isinstance(value, list) else []


def _wait_fresh_image(
    session: live_judge.CdpSession,
    state_dir: Path,
    before_sources: set[str],
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_src = ""
    stable = 0
    while time.monotonic() < deadline:
        if live_judge._history_limited(session):
            live_judge._raise_history_limit(state_dir)
        images = _image_sources(session)
        fresh = [
            x for x in images
            if isinstance(x, dict)
            and str(x.get("src") or "") not in before_sources
            and int(x.get("width") or 0) >= 512
            and int(x.get("height") or 0) >= 512
        ]
        if fresh:
            item = fresh[-1]
            src = str(item.get("src") or "")
            state = live_judge._rows(session)
            if src == last_src and not state.get("stopping"):
                stable += 1
            else:
                last_src = src
                stable = 0
            if stable >= 2:
                return item
        text = _latest_assistant_text(session)
        if re.search(r"cannot generate|can't generate|unable to generate|无法生成|不能生成|policy|安全政策", text, re.I):
            raise _error("GENERATOR_REJECTED", "ChatGPT image generation was rejected", response=text[:800])
        time.sleep(1.5)
    raise _error("WAITING_PROVIDER", "ChatGPT generated image did not become visible in time", retryable=True)


def _persist_generator_binding(
    run_dir: Path,
    run_id: str,
    target: dict[str, Any],
    session: live_judge.CdpSession,
    *,
    role_seeded: bool | None = None,
) -> dict[str, Any]:
    path = _binding_path(run_dir)
    binding = live_judge._read_json(path) or {
        "schema_version": "storyboard-chatgpt-web-provider-binding-1.0",
        "run_id": run_id,
    }
    prior = binding.get("generator") if isinstance(binding.get("generator"), dict) else {}
    state = live_judge._rows(session)
    observed_url = str(state.get("url") or "")
    observed_id = _conversation_id(observed_url)
    conversation_id = observed_id or str(prior.get("conversation_id") or "") or f"WEB:{target['id']}"
    route_kind = "stable" if observed_id else "ephemeral_web"
    binding["generator"] = {
        **prior,
        "role": "generator",
        "conversation_id": conversation_id,
        "conversation_url": observed_url or str(prior.get("conversation_url") or "https://chatgpt.com/"),
        "target_id": str(target["id"]),
        "websocket_debugger_url": str(target["webSocketDebuggerUrl"]),
        "route_kind": route_kind,
        "role_seeded": bool(prior.get("role_seeded")) if role_seeded is None else bool(role_seeded),
        "updated_at": time.time(),
    }
    live_judge._atomic_json(path, binding)
    return binding


def _seed_generator_role(
    session: live_judge.CdpSession,
    state_dir: Path,
) -> None:
    before = _assistant_count(session)
    seed = (
        "这是 Storyboard Forge 的 Generator 生图会话。"
        "你只负责按照后续每条任务生成或编辑一张图片；需要连续性时以随消息上传的图片为实际参考。"
        "不要审图，不要返回 Judge JSON。回复且只回复 GENERATOR_READY。"
    )
    live_judge._submit_text(session, seed)
    reply = _wait_assistant_after(session, state_dir, before, timeout_seconds=120)
    if "GENERATOR_READY" not in reply:
        raise _error("GENERATOR_BIND_FAILED", "Generator conversation did not acknowledge GENERATOR_READY", retryable=True)
    # A stable /c/<conversation_id> URL is useful for later exact asset recovery,
    # but it is not semantic authority and must not block Generator role binding.
    # Current ChatGPT Web can keep the page on an ephemeral route after the role
    # acknowledgement and establish a stable conversation only on the image turn.


def _open_generator_session(
    state_dir: Path,
    run_dir: Path,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Any], live_judge.CdpSession, bool]:
    live_judge._ensure_browser(state_dir)
    existing = live_judge._read_json(_binding_path(run_dir)) or {}
    saved = existing.get("generator") if isinstance(existing.get("generator"), dict) else {}

    stored_id = str(saved.get("conversation_id") or "")
    stored_url = str(saved.get("conversation_url") or "")
    websocket_url = str(saved.get("websocket_debugger_url") or "")
    target_id = str(saved.get("target_id") or "")

    target: dict[str, Any] | None = None
    session: live_judge.CdpSession | None = None
    reused = False

    if websocket_url and target_id:
        try:
            candidate = live_judge.CdpSession(websocket_url, timeout=30.0)
            live_judge._wait_composer(candidate, state_dir)
            observed_id = _conversation_id(str(live_judge._rows(candidate).get("url") or ""))
            if stored_id and not stored_id.startswith("WEB:") and observed_id != stored_id:
                candidate.close()
            else:
                session = candidate
                target = {"id": target_id, "webSocketDebuggerUrl": websocket_url, "keep_open": True}
                reused = True
        except Exception as exc:
            if live_judge._provider_gate_error(exc):
                raise
            try:
                candidate.close()
            except Exception:
                pass

    if session is None and stored_url and stored_id and not stored_id.startswith("WEB:"):
        opened = None
        try:
            live_judge._reserve_navigation(state_dir, "generator_stable_conversation_direct_reopen")
            opened = live_judge._new_target(stored_url)
            candidate = live_judge.CdpSession(str(opened["webSocketDebuggerUrl"]), timeout=30.0)
            live_judge._wait_composer(candidate, state_dir)
            observed_id = _conversation_id(str(live_judge._rows(candidate).get("url") or ""))
            if observed_id != stored_id:
                candidate.close()
                live_judge._close_target(str(opened["id"]))
                opened = None
            else:
                session = candidate
                target = {**opened, "keep_open": True}
                reused = True
        except Exception as exc:
            if opened is not None:
                live_judge._close_target(str(opened.get("id") or ""))
            if live_judge._provider_gate_error(exc):
                raise
            session = None
            target = None

    fresh = False
    if session is None:
        live_judge._reserve_fresh_chat(state_dir)
        opened = live_judge._new_target("https://chatgpt.com/")
        try:
            candidate = live_judge.CdpSession(str(opened["webSocketDebuggerUrl"]), timeout=30.0)
            live_judge._wait_composer(candidate, state_dir)
        except Exception:
            live_judge._close_target(str(opened["id"]))
            raise
        session = candidate
        target = {**opened, "keep_open": True}
        reused = False
        fresh = True

    if fresh or not bool(saved.get("role_seeded")):
        _seed_generator_role(session, state_dir)
        binding = _persist_generator_binding(run_dir, run_id, target, session, role_seeded=True)
    else:
        binding = _persist_generator_binding(run_dir, run_id, target, session)

    return binding, target, session, reused


def bind_generator(state_dir: Path, run_dir: Path, run_id: str) -> dict[str, Any]:
    binding, _target, session, reused = _open_generator_session(state_dir, run_dir, run_id)
    try:
        return {"already_bound": reused, "binding": binding}
    finally:
        session.close()


def _upload_files(session: live_judge.CdpSession, paths: list[Path]) -> None:
    if not paths:
        return
    node_id = live_judge._ensure_file_input(session)
    session.call("DOM.setFileInputFiles", {"files": [str(p) for p in paths], "nodeId": node_id})
    time.sleep(2.0)


def _fetch_conversation(session: live_judge.CdpSession, conversation_id: str) -> dict[str, Any]:
    # ChatGPT backend conversation endpoints require the same bearer used by the
    # signed-in web app. Resolve it inside the browser page and never return or
    # persist the token across the CDP boundary.
    script = f"""(async()=>{{
      const id={json.dumps(conversation_id)};
      let token='';
      try{{
        const auth=await fetch('/api/auth/session',{{credentials:'include',cache:'no-store'}});
        if(auth.ok){{
          const session=await auth.json();
          token=String(session?.accessToken||'');
        }}
      }}catch(e){{}}
      const headers={{'Accept':'application/json'}};
      if(token)headers['Authorization']='Bearer '+token;
      const urls=[
        '/backend-api/conversations/'+encodeURIComponent(id)+'?include_has_versions=true&num_turns=100',
        '/backend-api/conversations/'+encodeURIComponent(id),
        '/backend-api/conversation/'+encodeURIComponent(id)
      ];
      const statuses=[];
      for(const url of urls){{
        try{{
          const r=await fetch(url,{{credentials:'include',headers}});
          const text=await r.text();
          statuses.push(r.status);
          if(r.ok)return {{ok:true,status:r.status,text,statuses}};
          if(r.status===429)return {{ok:false,status:r.status,text:'',statuses}};
        }}catch(e){{}}
      }}
      return {{ok:false,status:statuses.length?statuses[statuses.length-1]:0,text:'',statuses}};
    }})()"""
    raw = session.evaluate(script) or {}
    status = int(raw.get("status") or 0)
    if status == 429:
        raise _error("HISTORY_RATE_LIMITED", "ChatGPT conversation endpoint rate-limited exact generator recovery", retryable=True)
    if not raw.get("ok"):
        label = str(status) if status else "unknown"
        raise _error(
            "WAITING_PROVIDER",
            f"Exact ChatGPT generator conversation JSON is unavailable (HTTP {label})",
            retryable=True,
            http_status=status or None,
        )
    try:
        value = json.loads(str(raw.get("text") or ""))
    except ValueError as exc:
        raise _error("GENERATOR_RECOVERY_INVALID", "ChatGPT conversation endpoint returned invalid JSON", retryable=True) from exc
    if not isinstance(value, dict):
        raise _error("GENERATOR_RECOVERY_INVALID", "ChatGPT conversation JSON is not an object", retryable=True)
    return value


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        parts = content.get("parts")
        if isinstance(parts, list):
            out = []
            for part in parts:
                if isinstance(part, str):
                    out.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    out.append(part["text"])
            return "\n".join(out)
        if isinstance(content.get("text"), str):
            return content["text"]
    return ""


def _flatten_messages(conv: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(conv.get("messages"), list):
        return [x for x in conv["messages"] if isinstance(x, dict)]
    mapping = conv.get("mapping")
    rows = []
    if isinstance(mapping, dict):
        for node in mapping.values():
            if isinstance(node, dict) and isinstance(node.get("message"), dict):
                rows.append(node["message"])
    rows.sort(key=lambda x: float(x.get("create_time") or 0))
    return rows


def _find_generated_asset(conv: dict[str, Any], operation_key: str) -> dict[str, Any] | None:
    messages = _flatten_messages(conv)
    start = -1
    for index, message in enumerate(messages):
        if operation_key in _message_text(message):
            start = index
    if start < 0:
        return None
    for message in messages[start + 1:]:
        content = message.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or part.get("content_type") != "image_asset_pointer":
                continue
            pointer = str(part.get("asset_pointer") or "")
            if pointer.startswith("sediment://"):
                kind = "sediment"
                file_id = pointer[len("sediment://"):]
            elif pointer.startswith("file-service://"):
                kind = "file-service"
                file_id = pointer[len("file-service://"):]
            else:
                continue
            if not FILE_ID.fullmatch(file_id):
                continue
            return {
                "message_id": str(message.get("id") or ""),
                "asset_pointer": pointer,
                "pointer_kind": kind,
                "file_id": file_id,
                "width": part.get("width") or ((part.get("metadata") or {}).get("generation") or {}).get("width"),
                "height": part.get("height") or ((part.get("metadata") or {}).get("generation") or {}).get("height"),
                "size_bytes": part.get("size_bytes"),
            }
    return None


def _download_asset(
    session: live_judge.CdpSession,
    conversation_id: str,
    asset: dict[str, Any],
) -> tuple[bytes, dict[str, Any]]:
    file_id = str(asset["file_id"])
    resolver = f"/backend-api/files/download/{file_id}?conversation_id={conversation_id}&inline=false"
    script = f"""(async()=>{{
      let token='';
      try{{
        const auth=await fetch('/api/auth/session',{{credentials:'include',cache:'no-store'}});
        if(auth.ok){{
          const session=await auth.json();
          token=String(session?.accessToken||'');
        }}
      }}catch(e){{}}
      const headers={{'Accept':'application/json'}};
      if(token)headers['Authorization']='Bearer '+token;
      const r=await fetch({json.dumps(resolver)},{{credentials:'include',headers}});
      const text=await r.text();
      return {{ok:r.ok,status:r.status,text}};
    }})()"""
    resolved = session.evaluate(script) or {}
    if not resolved.get("ok"):
        status = int(resolved.get("status") or 0)
        if status == 429:
            raise _error("HISTORY_RATE_LIMITED", "ChatGPT generated image resolver was rate-limited", retryable=True)
        raise _error("WAITING_PROVIDER", f"ChatGPT generated image resolver HTTP {status}", retryable=True)
    try:
        payload = json.loads(str(resolved.get("text") or ""))
    except ValueError as exc:
        raise _error("GENERATOR_ASSET_INVALID", "Generated image resolver returned non-JSON") from exc
    url = str(payload.get("download_url") or "")
    if not url:
        raise _error("GENERATOR_ASSET_INVALID", "Generated image resolver returned no download_url")
    script2 = f"""(async()=>{{
      let token='';
      try{{
        const auth=await fetch('/api/auth/session',{{credentials:'include',cache:'no-store'}});
        if(auth.ok){{
          const session=await auth.json();
          token=String(session?.accessToken||'');
        }}
      }}catch(e){{}}
      const headers={{}};
      if(token)headers['Authorization']='Bearer '+token;
      const r=await fetch({json.dumps(url)},{{credentials:'include',headers}});
      const ct=String(r.headers.get('content-type')||'').toLowerCase();
      const ab=await r.arrayBuffer();
      const u8=new Uint8Array(ab);
      let binary='';
      const chunk=0x8000;
      for(let i=0;i<u8.length;i+=chunk)binary+=String.fromCharCode(...u8.subarray(i,i+chunk));
      return {{ok:r.ok,status:r.status,content_type:ct,b64:btoa(binary)}};
    }})()"""
    downloaded = session.evaluate(script2) or {}
    if not downloaded.get("ok"):
        raise _error("WAITING_PROVIDER", f"Generated image download HTTP {int(downloaded.get('status') or 0)}", retryable=True)
    content_type = str(downloaded.get("content_type") or "")
    if not content_type.startswith("image/"):
        raise _error("GENERATOR_ASSET_INVALID", "Generated asset content-type is not image")
    try:
        image_bytes = base64.b64decode(str(downloaded.get("b64") or ""), validate=True)
    except Exception as exc:
        raise _error("GENERATOR_ASSET_INVALID", "Generated image base64 decode failed") from exc
    if not image_bytes:
        raise _error("GENERATOR_ASSET_INVALID", "Generated image download returned empty bytes")
    expected = payload.get("file_size_bytes")
    if isinstance(expected, int) and expected > 0 and len(image_bytes) != expected:
        raise _error(
            "GENERATOR_ASSET_INVALID",
            "Generated image byte length mismatch",
            expected_bytes=expected,
            actual_bytes=len(image_bytes),
        )
    return image_bytes, {
        "proof_kind": "chatgpt-web-exact-image-asset-pointer",
        "conversation_id": conversation_id,
        "message_id": asset.get("message_id"),
        "file_id": file_id,
        "pointer_kind": asset.get("pointer_kind"),
        "content_type": content_type,
        "file_name": payload.get("file_name"),
        "file_size_bytes": len(image_bytes),
        "asset_width": asset.get("width"),
        "asset_height": asset.get("height"),
    }


def _recover_exact(
    session: live_judge.CdpSession,
    conversation_id: str,
    operation_key: str,
    *,
    attempts: int = 3,
    sleep_seconds: float = 4.0,
) -> tuple[bytes, dict[str, Any]] | None:
    for index in range(attempts):
        conv = _fetch_conversation(session, conversation_id)
        asset = _find_generated_asset(conv, operation_key)
        if asset:
            return _download_asset(session, conversation_id, asset)
        if index + 1 < attempts:
            time.sleep(sleep_seconds)
    return None


def _wait_exact_generated_asset(
    session: live_judge.CdpSession,
    state_dir: Path,
    conversation_id: str,
    operation_key: str,
    timeout_seconds: int,
) -> tuple[bytes, dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    last_error: ExtensionError | None = None
    while True:
        if live_judge._history_limited(session):
            live_judge._raise_history_limit(state_dir)
        try:
            exact = _recover_exact(session, conversation_id, operation_key, attempts=1)
        except ExtensionError as exc:
            if live_judge._provider_gate_error(exc):
                raise
            if str(getattr(exc, "code", "")) not in {"WAITING_PROVIDER", "GENERATOR_RECOVERY_INVALID"}:
                raise
            last_error = exc
        else:
            if exact:
                return exact
            last_error = None

        text = _latest_assistant_text(session)
        if re.search(r"cannot generate|can't generate|unable to generate|无法生成|不能生成|policy|安全政策", text, re.I):
            raise _error("GENERATOR_REJECTED", "ChatGPT image generation was rejected", response=text[:800])

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            details = {}
            if last_error is not None:
                details["last_recovery_error"] = str(last_error)[:500]
            raise _error(
                "WAITING_PROVIDER",
                "Exact ChatGPT image_asset_pointer bytes did not become recoverable in time",
                retryable=True,
                **details,
            )
        time.sleep(min(EXACT_RECOVERY_POLL_SECONDS, remaining))


def generate_image(
    state_dir: Path,
    run_dir: Path,
    *,
    operation_key: str,
    prompt: str,
    reference_paths: list[Path],
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    if not HEX64.fullmatch(operation_key):
        raise _error("OPERATION_KEY_INVALID", "Generator operation_key must be a SHA-256 hex string")
    run_record = live_judge._read_json(run_dir / "run.json") or {}
    run_id = str(run_record.get("run_id") or run_dir.name)

    binding, target, session, _ = _open_generator_session(state_dir, run_dir, run_id)
    try:
        generator = binding.get("generator") or {}
        conversation_id = str(generator.get("conversation_id") or "")
        if conversation_id and not conversation_id.startswith("WEB:"):
            try:
                recovered = _recover_exact(session, conversation_id, operation_key, attempts=1)
            except ExtensionError as exc:
                if live_judge._provider_gate_error(exc):
                    raise
                if str(getattr(exc, "code", "")) not in {"WAITING_PROVIDER", "GENERATOR_RECOVERY_INVALID"}:
                    raise
                recovered = None
            if recovered:
                image_bytes, receipt = recovered
                return {"recovered": True, "submitted": True, "bytes": image_bytes, "receipt": receipt, "binding": generator}
            # Pragmatic transport: a visible old operation is only a recovery hint.
            # If exact bytes are not recoverable, resend the same task instead of
            # turning browser transcript state into a hard generation veto.

        _upload_files(session, reference_paths)
        full_prompt = f"operation_key={operation_key}\n\n{prompt}"
        live_judge._submit_text(session, full_prompt)

        deadline = time.monotonic() + 30.0
        confirmed = False
        while time.monotonic() < deadline:
            if live_judge._history_limited(session):
                live_judge._raise_history_limit(state_dir)
            state = live_judge._rows(session)
            confirmed, _ = live_judge._find_operation(state, operation_key)
            if confirmed:
                break
            time.sleep(0.5)
        if not confirmed:
            raise _error("WAITING_PROVIDER", "Generator send was not confirmed in current ChatGPT page", retryable=True)

        # DOM image layout is presentation-only and changes independently of
        # ChatGPT's exact stored asset. Stable conversation identity and exact
        # image_asset_pointer recovery remain the post-submit hard constraints.
        stable_url = _wait_stable_url(session)
        conversation_id = _conversation_id(stable_url)
        if not conversation_id:
            raise _error("WAITING_PROVIDER", "Generator conversation lacks stable identity after image generation", retryable=True)

        binding = _persist_generator_binding(run_dir, run_id, target, session, role_seeded=True)
        image_bytes, receipt = _wait_exact_generated_asset(
            session,
            state_dir,
            conversation_id,
            operation_key,
            timeout_seconds,
        )
        return {
            "recovered": False,
            "submitted": True,
            "bytes": image_bytes,
            "receipt": receipt,
            "binding": binding["generator"],
        }
    finally:
        session.close()

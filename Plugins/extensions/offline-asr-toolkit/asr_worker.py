"""One-shot, offline model worker used only by the Offline ASR Toolkit plugin.

Not an MCP action and not a general command executor. The trusted extension
creates the job specification inside the selected workspace output tree.
"""
from __future__ import annotations

import datetime as dt
import importlib.metadata as metadata
import json
import os
import socket
import sys
import traceback
from pathlib import Path
from typing import Any


def _offline_network_guard() -> None:
    def blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("OFFLINE_ASR_NETWORK_DISABLED: network connection attempted")
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.create_connection = blocked
    socket.getaddrinfo = blocked


def _version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def _funasr(spec: dict[str, Any]) -> tuple[str, list[Any], dict[str, Any]]:
    import torch
    from funasr import AutoModel

    device = spec["device"]
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    elif device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA requested but unavailable")
    options: dict[str, Any] = {
        "model": spec["model_dir"],
        "device": device,
        "disable_update": True,
        "trust_remote_code": False,
    }
    if spec.get("vad_dir"):
        options["vad_model"] = spec["vad_dir"]
    if spec.get("punctuation_dir"):
        options["punc_model"] = spec["punctuation_dir"]
    model = AutoModel(**options)
    rows = model.generate(input=spec["input_file"], batch_size_s=60)
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("FunASR returned no recognition candidates")
    parts: list[str] = []
    timestamps: list[Any] = []
    for row in rows[:256]:
        if not isinstance(row, dict):
            continue
        value = row.get("text")
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        values = row.get("timestamp")
        if isinstance(values, list):
            timestamps.extend(values[:2000])
    if not parts:
        raise RuntimeError("FunASR returned empty recognized text")
    return "\n".join(parts), timestamps[:4000], {
        "backend_version": _version("funasr"),
        "torch_version": _version("torch"),
        "modelscope_version": _version("modelscope"),
        "actual_device": device,
    }


def _faster_whisper(spec: dict[str, Any]) -> tuple[str, list[Any], dict[str, Any]]:
    from faster_whisper import WhisperModel

    device = spec["device"]
    if device == "auto":
        device = "auto"
    lang = None if spec["language"] == "auto" else spec["language"]
    if lang == "zh":
        lang = "zh"
    model = WhisperModel(spec["model_dir"], device=device, compute_type="auto", local_files_only=True)
    segments, info = model.transcribe(
        spec["input_file"], language=lang, beam_size=5, vad_filter=False,
        word_timestamps=True,
    )
    texts: list[str] = []
    stamps: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        if index >= 10000:
            raise RuntimeError("Abnormally many ASR segments; refusing unbounded output")
        if segment.text.strip():
            texts.append(segment.text.strip())
        stamps.append({
            "start": segment.start,
            "end": segment.end,
            "text": segment.text[:4000],
            "words": [
                {"start": word.start, "end": word.end, "word": word.word}
                for word in (segment.words or [])[:120]
            ],
        })
    if not texts:
        raise RuntimeError("Whisper produced no nonempty text")
    return "\n".join(texts), stamps, {
        "backend_version": _version("faster-whisper"),
        "torch_version": _version("torch"),
        "modelscope_version": _version("modelscope"),
        "language_probability": getattr(info, "language_probability", None),
        "recognized_language": getattr(info, "language", None),
    }


def run(job_path: Path) -> int:
    spec = json.loads(job_path.read_text(encoding="utf-8"))
    result_path = Path(spec["result_file"])
    record: dict[str, Any] = {
        "schema": "offline-asr-worker-result-v1",
        "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "engine": spec["engine"],
        "ok": False,
        "text": "",
    }
    exit_code = 2
    try:
        if spec["engine"] not in {"funasr", "faster-whisper"}:
            raise ValueError("Unsupported fixed ASR engine")
        _offline_network_guard()
        if spec["engine"] == "funasr":
            text, timestamps, versions = _funasr(spec)
        else:
            text, timestamps, versions = _faster_whisper(spec)
        record.update({
            "ok": True,
            "text": text,
            "timestamps": timestamps,
            **versions,
        })
        exit_code = 0
    except BaseException as exc:
        record["error"] = str(exc)[:5000]
        record["traceback"] = traceback.format_exc()[-15000:]
        print(record["traceback"], file=sys.stderr)
    finally:
        record["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        result_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_file():
        print("One trusted job JSON path required", file=sys.stderr)
        sys.exit(64)
    sys.exit(run(Path(sys.argv[1])))

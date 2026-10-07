"""MCP/KIS API 호출 로깅 — 모든 도구/HTTP 호출을 추적."""

import asyncio
import json
import logging
import time
import functools
from datetime import datetime
from pathlib import Path

from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

MCP_LOG_FILE = PROJECT_ROOT / "logs" / "mcp_calls.json"
API_LOG_FILE = PROJECT_ROOT / "logs" / "api_calls.json"


def _ensure_dir():
    MCP_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def _append(file: Path, entry: dict):
    _ensure_dir()
    entries = []
    if file.exists():
        try:
            entries = json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            entries = []
    entries.append(entry)
    # 최근 500건만 유지
    if len(entries) > 500:
        entries = entries[-500:]
    file.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def _load(file: Path, count: int = 50) -> list[dict]:
    if not file.exists():
        return []
    try:
        entries = json.loads(file.read_text(encoding="utf-8"))
        return entries[-count:]
    except json.JSONDecodeError:
        return []


# ── MCP 도구 로깅 ──

def log_mcp_call(tool_name: str, inputs: dict, output: str, duration_ms: int, success: bool):
    """MCP 도구 호출 기록."""
    _append(MCP_LOG_FILE, {
        "timestamp": datetime.now().isoformat(),
        "type": "mcp",
        "tool": tool_name,
        "input": inputs,
        "output_preview": output[:200] if output else "",
        "duration_ms": duration_ms,
        "success": success,
    })


def mcp_logged(func):
    """MCP 도구 함수에 자동 로깅을 적용하는 데코레이터 (sync/async 모두 지원)."""
    if asyncio.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start = time.time()
            try:
                result = await func(*args, **kwargs)
                duration = int((time.time() - start) * 1000)
                log_mcp_call(func.__name__, kwargs or _args_to_dict(func, args), str(result), duration, True)
                return result
            except Exception as e:
                duration = int((time.time() - start) * 1000)
                log_mcp_call(func.__name__, kwargs or _args_to_dict(func, args), str(e), duration, False)
                raise
        return async_wrapper
    else:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start = time.time()
            try:
                result = func(*args, **kwargs)
                duration = int((time.time() - start) * 1000)
                log_mcp_call(func.__name__, kwargs or _args_to_dict(func, args), str(result), duration, True)
                return result
            except Exception as e:
                duration = int((time.time() - start) * 1000)
                log_mcp_call(func.__name__, kwargs or _args_to_dict(func, args), str(e), duration, False)
                raise
        return wrapper


def _args_to_dict(func, args) -> dict:
    """위치 인자를 dict로 변환."""
    import inspect
    sig = inspect.signature(func)
    params = list(sig.parameters.keys())
    return {params[i]: args[i] for i in range(min(len(args), len(params)))}


# ── KIS API HTTP 로깅 ──

def log_api_call(method: str, path: str, tr_id: str, params: dict,
                 status: int, duration_ms: int, server: str):
    """KIS API HTTP 호출 기록."""
    _append(API_LOG_FILE, {
        "timestamp": datetime.now().isoformat(),
        "type": "kis_api",
        "method": method,
        "path": path.split("/")[-1] if "/" in path else path,  # 마지막 segment만
        "tr_id": tr_id,
        "params_preview": {k: v for k, v in list(params.items())[:5]},
        "status": status,
        "duration_ms": duration_ms,
        "server": server,
    })


# ── 조회 ──

def get_mcp_logs(count: int = 50) -> list[dict]:
    return _load(MCP_LOG_FILE, count)


def get_api_logs(count: int = 50) -> list[dict]:
    return _load(API_LOG_FILE, count)


def get_all_logs(count: int = 50) -> list[dict]:
    """MCP + API 로그 합쳐서 시간순 정렬."""
    all_logs = get_mcp_logs(count) + get_api_logs(count)
    all_logs.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return all_logs[:count]

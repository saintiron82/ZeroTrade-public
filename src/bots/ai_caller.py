"""봇용 AI 호출 헬퍼 — agentcli 브릿지.

모든 봇이 이 모듈을 통해 AI를 호출하면, 독립 패키지 `agentcli`의
LLMClient를 경유하여 프로바이더/모델을 통일 관리.

agentcli는 FF의 venv에 editable install되어 있다고 가정 (start.sh의 .venv).
설치: `pip install -e /Users/saintiron/Projects/agentcli`
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

from src.config import PROJECT_ROOT

from agentcli import LLMClient, MemoryStore
from agentcli.types import StreamChunk
from agentcli.providers.registry import create_default_registry

logger = logging.getLogger(__name__)

# 토큰 사용량 로그 파일 (레거시 호환)
TOKEN_LOG = PROJECT_ROOT / "logs" / "token_usage.jsonl"
TOKEN_LOG.parent.mkdir(parents=True, exist_ok=True)

# LLMClient 싱글톤
_llm_client: LLMClient | None = None


@dataclass
class AIResult:
    content: str
    provider: str
    model: str
    tokens: int
    latency_ms: int
    error: str = ""
    error_type: str = ""


def _get_llm_client() -> LLMClient:
    """LLMClient 싱글톤 반환.

    MemoryStore는 TTL/cap으로 장기 실행 시 메모리 누수 방지.
    봇이 `bot:{bot_id}` 안정 키로 대화를 이어가므로 용량 캡은 봇 수의 배수면 충분.
    """
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient(
            store=MemoryStore(max_conversations=200, ttl_hours=24),
            registry=create_default_registry(),
        )
    return _llm_client


def _bool_param(params: dict, key: str, default: bool) -> bool:
    value = params.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off")
    return bool(value)


def _normalize_model(provider: str, model: str) -> str:
    """Drop model names that clearly belong to another provider."""
    provider_l = (provider or "").lower()
    model = model or ""
    model_l = model.lower()
    if provider_l == "codex" and any(k in model_l for k in ("claude", "sonnet", "haiku", "opus")):
        logger.warning("[AI] codex provider에 호환되지 않는 model=%s 지정, 기본 모델 사용", model)
        return ""
    if provider_l == "claude" and any(k in model_l for k in ("gpt", "codex")):
        logger.warning("[AI] claude provider에 호환되지 않는 model=%s 지정, 기본 모델 사용", model)
        return ""
    return model


def _resolve_params(params: dict, bot_id: str, conversation_id: str, alias: str):
    """파라미터 정규화: provider/model + 안정 키 정책.

    우선순위:
      1. 명시 conversation_id — 그대로 사용
      2. 명시 alias — conversation_id는 비워두고 alias로 resolve (Client 측 처리)
      3. bot_id — `bot:{bot_id}`를 conversation_id로 자동 사용
    """
    provider = params.get("ai_provider", "codex")
    model = _normalize_model(provider, params.get("ai_model", ""))
    if not conversation_id and not alias and bot_id:
        conversation_id = f"bot:{bot_id}"
    return provider, model, conversation_id


def _response_field(response, name: str, default=""):
    """LLM 응답이 객체/딕셔너리 어느 쪽이어도 안전하게 값을 꺼낸다."""
    if response is None:
        return default
    if isinstance(response, dict):
        return response.get(name, default)
    return getattr(response, name, default)


def _token_total(response) -> int:
    tokens = _response_field(response, "tokens", None)
    if tokens is None:
        return 0
    if isinstance(tokens, dict):
        value = tokens.get("total_tokens", tokens.get("total", 0))
    else:
        value = getattr(tokens, "total_tokens", 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def invoke_ai_result(
    prompt: str,
    params: dict,
    *,
    timeout: int = 180,
    bot_id: str = "",
    conversation_id: str = "",
    alias: str = "",
    cwd: str | None = None,
) -> AIResult:
    """봇에서 AI CLI를 호출하는 공통 함수. 실패 시 자동 폴백.

    Args:
        prompt: AI에 보낼 프롬프트
        params: 봇 파라미터 (ai_provider, ai_model)
        timeout: 타임아웃 (초)
        bot_id: 호출한 봇 ID (토큰 추적용, 안정 키 자동 생성)
        conversation_id: 대화 이어쓰기 키. 미지정 시 `f"bot:{bot_id}"` 자동 사용.
        alias: 대화 별칭. 지정 시 conversation_id 대신 alias로 resolve.
        cwd: CLI 서브프로세스 작업 디렉토리.

    Returns:
        AI 출력과 감사용 메타데이터. 모든 프로바이더 실패 시 content는 빈 문자열.
    """
    params = params or {}
    provider, model, conversation_id = _resolve_params(
        params, bot_id, conversation_id, alias)
    fallback = _bool_param(params, "ai_fallback_enabled", True)
    client = _get_llm_client()
    started = time.monotonic()
    response = client.chat(
        prompt,
        provider=provider, model=model,
        owner=bot_id or "unknown",
        conversation_id=conversation_id, alias=alias,
        timeout=timeout, cwd=cwd,
        fallback=fallback,
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    content = str(_response_field(response, "content", "") or "")
    response_provider = str(_response_field(response, "provider", "") or provider)
    response_model = str(_response_field(response, "model", "") or model)
    error = str(_response_field(response, "error", "") or "")
    error_type = str(_response_field(response, "error_type", "") or "")
    tokens = _token_total(response)

    # 레거시 토큰 로깅 (JSONL)
    if tokens > 0:
        _log_token_usage(
            bot_id or "unknown",
            response_provider,
            response_model,
            tokens,
            len(prompt),
            len(content),
        )

    if not content:
        logger.warning(
            "[AI] 응답 없음 bot=%s provider=%s error=%s",
            bot_id or "unknown",
            response_provider,
            error or error_type or "no-content",
        )

    return AIResult(
        content=content,
        provider=response_provider,
        model=response_model,
        tokens=tokens,
        latency_ms=latency_ms,
        error=error,
        error_type=error_type,
    )


def invoke_ai(
    prompt: str,
    params: dict,
    *,
    timeout: int = 180,
    bot_id: str = "",
    conversation_id: str = "",
    alias: str = "",
    cwd: str | None = None,
) -> str:
    """기존 호출자 호환용: AI 출력 텍스트만 반환."""
    return invoke_ai_result(
        prompt,
        params,
        timeout=timeout,
        bot_id=bot_id,
        conversation_id=conversation_id,
        alias=alias,
        cwd=cwd,
    ).content


async def invoke_ai_async(prompt: str, params: dict, *,
                          timeout: int = 180, bot_id: str = "",
                          conversation_id: str = "", alias: str = "",
                          cwd: str | None = None) -> str:
    """invoke_ai의 비동기 버전. 여러 봇을 `asyncio.gather`로 병렬 실행 가능.

    사용 예:
        results = await asyncio.gather(
            invoke_ai_async("A 분석", params, bot_id="bull"),
            invoke_ai_async("B 분석", params, bot_id="bear"),
            invoke_ai_async("판단", params, bot_id="trader"),
        )
    """
    provider, model, conversation_id = _resolve_params(
        params, bot_id, conversation_id, alias)
    fallback = _bool_param(params, "ai_fallback_enabled", True)
    client = _get_llm_client()
    response = await client.chat_async(
        prompt,
        provider=provider, model=model,
        owner=bot_id or "unknown",
        conversation_id=conversation_id, alias=alias,
        timeout=timeout, cwd=cwd,
        fallback=fallback,
    )
    if response.tokens.total_tokens > 0:
        _log_token_usage(
            bot_id or "unknown", response.provider, response.model,
            response.tokens.total_tokens, len(prompt), len(response.content))
    if not response.content:
        logger.warning(
            "[AI] 응답 없음 bot=%s provider=%s fallback=%s error=%s",
            bot_id or "unknown", response.provider or provider, fallback,
            response.error or response.error_type or "no-content",
        )
    return response.content


async def invoke_ai_stream(prompt: str, params: dict, *,
                           timeout: int = 180, bot_id: str = "",
                           conversation_id: str = "", alias: str = "",
                           cwd: str | None = None) -> AsyncIterator[StreamChunk]:
    """AI CLI 응답을 증분 청크로 스트리밍.

    yield되는 청크 타입: text / thinking / tool_use / tool_result / event / error / done

    사용 예:
        async for chunk in invoke_ai_stream("복잡한 분석", params, bot_id="x"):
            if chunk.type == "text":
                print(chunk.content, end="", flush=True)
            elif chunk.type == "done":
                total = chunk.usage.total_tokens if chunk.usage else 0
                print(f"\\n[tokens={total}]")
    """
    provider, model, conversation_id = _resolve_params(
        params, bot_id, conversation_id, alias)
    fallback = _bool_param(params, "ai_fallback_enabled", True)
    client = _get_llm_client()
    total_tokens = 0
    full_output = ""
    async for chunk in client.chat_stream(
            prompt,
            provider=provider, model=model,
            owner=bot_id or "unknown",
            conversation_id=conversation_id, alias=alias,
            timeout=timeout, cwd=cwd,
            fallback=fallback):
        if chunk.type == "done":
            full_output = chunk.content
            if chunk.usage:
                total_tokens = chunk.usage.total_tokens
        yield chunk
    if total_tokens > 0:
        _log_token_usage(
            bot_id or "unknown", provider, model,
            total_tokens, len(prompt), len(full_output))


def get_token_stats(bot_id: str = "", days: int = 7) -> dict:
    """토큰 사용 통계 조회 (레거시 JSONL 기반)."""
    if not TOKEN_LOG.exists():
        return {"total_tokens": 0, "total_calls": 0, "by_bot": {}}

    cutoff = (datetime.now() - __import__("datetime").timedelta(days=days)).isoformat()
    total_tokens = 0
    total_calls = 0
    by_bot: dict[str, dict] = {}

    for line in TOKEN_LOG.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            if entry["timestamp"] < cutoff:
                continue
            if bot_id and entry.get("bot_id") != bot_id:
                continue

            tokens = entry.get("tokens", 0)
            bid = entry.get("bot_id", "unknown")
            total_tokens += tokens
            total_calls += 1

            if bid not in by_bot:
                by_bot[bid] = {"tokens": 0, "calls": 0}
            by_bot[bid]["tokens"] += tokens
            by_bot[bid]["calls"] += 1
        except (json.JSONDecodeError, KeyError):
            pass

    return {
        "total_tokens": total_tokens,
        "total_calls": total_calls,
        "by_bot": by_bot,
    }


def _log_token_usage(bot_id: str, provider: str, model: str, tokens: int,
                     prompt_len: int, output_len: int):
    """토큰 사용량을 JSONL 파일에 기록."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "bot_id": bot_id,
        "provider": provider,
        "model": model,
        "tokens": tokens,
        "prompt_chars": prompt_len,
        "output_chars": output_len,
    }
    try:
        with open(TOKEN_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
    logger.info("[토큰] %s (%s): %s tokens", bot_id, provider, f"{tokens:,}")

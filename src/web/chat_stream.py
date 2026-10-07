"""Claude CLI 스트리밍 채팅 — agentcli 기반 SSE 생성.

프론트엔드 채팅의 실시간 스트리밍 엔드포인트(`POST /chat/stream`)용.
이전의 subprocess 직접 구현을 제거하고 agentcli.LLMClient.chat_stream()을 경유.

외부 인터페이스(`stream_claude_chat(prompt, session_id, model)`)와 SSE 이벤트
포맷(`text`/`tool_use`/`tool_result`/`done`/`error`)은 기존과 동일 — 프론트엔드는 수정 불요.
"""

import json
import logging
from pathlib import Path

from agentcli import LLMClient, MemoryStore
from agentcli.providers.registry import create_default_registry

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 스트리밍 채팅 전용 LLMClient 싱글톤
_stream_client: LLMClient | None = None


def _get_client() -> LLMClient:
    global _stream_client
    if _stream_client is None:
        _stream_client = LLMClient(
            store=MemoryStore(max_conversations=500, ttl_hours=24),
            registry=create_default_registry(),
        )
    return _stream_client


async def stream_claude_chat(prompt: str, session_id: str = "", model: str = ""):
    """Claude (또는 기타 agentic CLI)를 스트리밍 모드로 실행 + SSE 생성.

    Returns:
        (async_generator_factory, used_session_id)
    """
    client = _get_client()
    # session_id가 비어 있으면 agentcli가 UUID 발급 → resp에서 받아 반환
    # session_id가 있으면 conversation_id로 재사용, provider가 --resume 수행
    conv_id = session_id or ""

    async def generate():
        final_sid = session_id or ""
        try:
            async for chunk in client.chat_stream(
                prompt,
                provider="claude",
                model=model,
                conversation_id=conv_id,
                owner="chat-frontend",
                cwd=str(_PROJECT_ROOT),
                timeout=600,
            ):
                if chunk.type == "text":
                    yield _sse("text", {"type": "text", "delta": chunk.content})
                elif chunk.type == "tool_use":
                    data = chunk.data or {}
                    yield _sse("tool_use", {
                        "type": "tool_use",
                        "tool_name": data.get("name", ""),
                        "tool_id": data.get("id", ""),
                        "input": data.get("input", {}),
                    })
                elif chunk.type == "tool_result":
                    data = chunk.data or {}
                    yield _sse("tool_result", {
                        "type": "tool_result",
                        "tool_id": data.get("tool_use_id", ""),
                        "content": str(data.get("content", ""))[:500],
                    })
                elif chunk.type == "done":
                    usage = chunk.usage
                    if chunk.session_id:
                        final_sid = chunk.session_id
                    yield _sse("done", {
                        "type": "done",
                        "text": chunk.content,
                        "usage": {
                            "input_tokens": usage.prompt_tokens if usage else 0,
                            "output_tokens": usage.completion_tokens if usage else 0,
                            "cached_tokens": usage.cached_tokens if usage else 0,
                        },
                        "session_id": final_sid,
                    })
                elif chunk.type == "error":
                    yield _sse("error", {
                        "type": "error",
                        "message": chunk.content or "스트리밍 오류",
                    })
                # thinking / event는 SSE로 내보내지 않음 (기존 동작 유지)
        except Exception as e:
            logger.error("스트리밍 오류: %s", e)
            yield _sse("error", {"type": "error", "message": str(e)})
        finally:
            # 프론트엔드(index.html)는 별도 `session` 이벤트로 session_id를
            # localStorage에 영속화하고 EventSource를 닫는다. 이 이벤트가 없으면
            # 매 요청마다 빈 session_id가 전송돼 agentcli가 새 대화를 만들고
            # 채팅 히스토리가 단절된다. 성공/실패 경로 모두에서 발사 보장.
            yield _sse("session", {"session_id": final_sid})

    # agentcli가 실제 session_id를 발급/재사용하는 건 런타임 중이므로
    # 리턴은 호출 시점의 conversation_id (혹은 기존 session_id)를 그대로 반환.
    # 실제 발급 sid는 stream 내 done 청크에서 프론트로 전달됨.
    return generate, (session_id or conv_id)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

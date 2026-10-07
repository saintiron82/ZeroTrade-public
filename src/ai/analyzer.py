"""AI 분석기 — agentcli 기반 멀티 프로바이더 + 모델 선택.

프론트엔드 채팅(`POST /chat`, `POST /chat/stream`)의 AI 호출 진입점.
이전의 subprocess 직접 구현을 제거하고 agentcli 라이브러리를 경유.

외부 인터페이스는 기존과 동일:
  AIAnalyzer(provider, model, timeout).analyze(prompt, session_id) → (text, sid)

session_id는 agentcli의 conversation_id 역할도 겸한다 (1:1 매핑).
agentcli는 FF의 venv에 editable install 되어 있어야 한다.
"""

import logging
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

from agentcli import LLMClient, MemoryStore
from agentcli.providers.registry import create_default_registry


class AIProvider(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    COPILOT = "copilot"


# 프로바이더별 선택 가능 모델 목록 (UI 드롭다운용) — 기존 유지
PROVIDER_MODELS: dict[str, list[dict]] = {
    "claude": [
        {"id": "", "name": "기본 (자동)"},
        {"id": "sonnet", "name": "Sonnet"},
        {"id": "opus", "name": "Opus"},
        {"id": "haiku", "name": "Haiku"},
    ],
    "codex": [
        {"id": "", "name": "기본 (자동)"},
        {"id": "o4-mini", "name": "o4-mini"},
        {"id": "o3", "name": "o3"},
        {"id": "gpt-4.1", "name": "GPT-4.1"},
        {"id": "gpt-5-codex", "name": "GPT-5 Codex"},
    ],
    "copilot": [
        {"id": "", "name": "기본 (자동)"},
        {"id": "claude-sonnet-4.6", "name": "Claude Sonnet 4.6"},
        {"id": "gpt-4o", "name": "GPT-4o"},
        {"id": "gpt-5", "name": "GPT-5"},
        {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
    ],
}


# 모듈 레벨 LLMClient 싱글톤 — 프로세스 수명 동안 재사용.
# routes.py가 여러 채팅 세션을 돌리므로 conversation cap은 넉넉히.
_client: LLMClient | None = None


def _get_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient(
            store=MemoryStore(max_conversations=500, ttl_hours=24),
            registry=create_default_registry(),
        )
    return _client


class AIAnalyzer:
    """전용 투자 에이전트 — agentcli를 경유한 멀티 프로바이더 호출.

    내부적으로 agentcli.LLMClient를 사용하여:
      - Claude Code / Codex / Copilot CLI를 통합 호출
      - 세션 자동 관리 (첫 호출 발급, 이후 --resume)
      - 토큰·드리프트 추적
    """

    def __init__(self, provider: AIProvider = AIProvider.CLAUDE,
                 model: str = "", timeout: int = 600):
        self._provider = provider
        self._model = model
        self._timeout = timeout

    def analyze(self, prompt: str, session_id: str = "") -> tuple[str, str]:
        """에이전트에게 프롬프트를 보내고 응답을 받음.

        Args:
            prompt: 사용자 메시지
            session_id: 이어쓸 session_id (빈 문자열이면 신규 세션).
                이 값은 agentcli의 conversation_id로도 사용되어 세션 메타데이터
                (Claude/Copilot이 발급한 실제 CLI session_id)가 저장된다.

        Returns:
            (응답 텍스트, 사용된 session_id)
            신규 호출 시 agentcli가 provider로부터 발급받은 session_id 반환.
            실패 시 빈 문자열 반환.
        """
        client = _get_client()
        # conversation_id로 session_id를 재사용 (1:1 매핑).
        # 빈 문자열이면 agentcli가 UUID를 새로 발급.
        conv_id = session_id or ""

        model_label = f" model={self._model}" if self._model else ""
        logger.info("%s 호출 시작:%s prompt=%s... session=%s timeout=%ds",
                    self._provider.value, model_label, prompt[:50],
                    (session_id or "신규")[:8], self._timeout)

        try:
            resp = client.chat(
                prompt,
                provider=self._provider.value,
                model=self._model,
                conversation_id=conv_id,
                owner="chat-frontend",
                timeout=self._timeout,
                cwd=str(PROJECT_ROOT),
            )
        except Exception as e:
            logger.error("AI 호출 예외 (%s): %s", self._provider.value, e)
            return "", session_id or ""

        if not resp.content:
            logger.error("%s 응답 없음 (session=%s)",
                         self._provider.value, (session_id or "신규")[:8])
            return "", resp.session_id or session_id or ""

        logger.info("%s 응답 완료 (%dms, %d자): %s...",
                    self._provider.value, resp.latency_ms,
                    len(resp.content), resp.content[:80])
        # provider가 발급/재사용한 실제 CLI session_id를 돌려줌
        return resp.content, resp.session_id or session_id or ""

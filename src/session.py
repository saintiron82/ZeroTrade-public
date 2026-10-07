"""대화 세션 관리 — 채팅 이력 저장/로드."""

import json
import logging
from datetime import datetime
from pathlib import Path

from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

SESSIONS_DIR = PROJECT_ROOT / "data" / "sessions"


class SessionManager:
    """채팅 세션 CRUD + 이력 관리."""

    def __init__(self):
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    def create(self, title: str = "", provider: str = "claude") -> dict:
        """새 세션 생성."""
        existing = sorted(SESSIONS_DIR.glob("session_*.json"))
        max_num = 0
        for f in existing:
            try:
                max_num = max(max_num, int(f.stem.split("_")[1]))
            except (IndexError, ValueError):
                pass
        sid = f"session_{max_num + 1:03d}"
        session = {
            "id": sid,
            "claude_session_id": "",  # Claude Code 내장 세션 ID (resume용)
            "provider": provider,
            "title": title or "새 대화",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "messages": [],
        }
        self._save(sid, session)
        return session

    def get(self, session_id: str) -> dict | None:
        path = SESSIONS_DIR / f"{session_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def add_message(self, session_id: str, role: str, content: str, provider: str = "") -> dict:
        """메시지 추가 (role: user/assistant)."""
        session = self.get(session_id)
        if not session:
            session = self.create()
            session_id = session["id"]

        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }
        if provider:
            msg["provider"] = provider
        session["messages"].append(msg)
        session["updated_at"] = datetime.now().isoformat()

        # 첫 사용자 메시지로 제목 자동 설정
        if session["title"] == "새 대화" and role == "user":
            session["title"] = content[:30] + ("..." if len(content) > 30 else "")

        self._save(session_id, session)
        return session

    def list_sessions(self, provider: str = "") -> list[dict]:
        """세션 목록 (최근순)."""
        sessions = []
        for f in sorted(SESSIONS_DIR.glob("session_*.json"), reverse=True):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if provider and data.get("provider", "claude") != provider:
                    continue
                sessions.append({
                    "id": data["id"],
                    "title": data["title"],
                    "provider": data.get("provider", "claude"),
                    "created_at": data["created_at"],
                    "updated_at": data["updated_at"],
                    "message_count": len(data["messages"]),
                })
            except (json.JSONDecodeError, KeyError):
                pass
        return sessions

    def delete(self, session_id: str):
        path = SESSIONS_DIR / f"{session_id}.json"
        if path.exists():
            path.unlink()

    def build_history_prompt(self, session_id: str, max_turns: int = 10) -> str:
        """세션의 최근 대화를 프롬프트 형태로 변환."""
        session = self.get(session_id)
        if not session or not session["messages"]:
            return ""

        recent = session["messages"][-max_turns * 2:]  # user+assistant 쌍
        lines = ["## 이전 대화"]
        for msg in recent:
            role = "사용자" if msg["role"] == "user" else "AI"
            # AI 응답이 길면 축약
            content = msg["content"]
            if msg["role"] == "assistant" and len(content) > 500:
                content = content[:500] + "...(생략)"
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    def set_claude_session_id(self, session_id: str, claude_session_id: str):
        """Claude Code 세션 ID 설정."""
        session = self.get(session_id)
        if session:
            session["claude_session_id"] = claude_session_id
            self._save(session_id, session)

    def _save(self, session_id: str, data: dict):
        path = SESSIONS_DIR / f"{session_id}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

"""Tests for outer-session ↔ agentcli-session mapping in /api/chat/stream.

Architecture:
- SessionManager stores file-backed `session_NNN` for the UI; each one carries
  a `claude_session_id` field pointing at the agentcli UUID conversation.
- agentcli only knows its UUID. When it issues a fresh UUID (no prior session),
  the routes wrapper MUST capture it from the done chunk and persist it via
  `SessionManager.set_claude_session_id`. Otherwise the next chat request
  passes an empty `claude_session_id` to agentcli and history is lost.

This guards the persistence step.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    sd = tmp_path / "sessions"
    sd.mkdir()
    import src.session as sess_mod
    monkeypatch.setattr(sess_mod, "SESSIONS_DIR", sd)
    return sd


@pytest.mark.asyncio
async def test_done_session_id_persisted_to_outer_session(session_dir, monkeypatch):
    """When agentcli emits a fresh session_id in `done`, the wrapper saves it
    onto the outer session_NNN as claude_session_id.
    """
    import src.session as sess_mod
    mgr = sess_mod.SessionManager()
    outer = mgr.create(provider="claude")
    outer_id = outer["id"]
    assert outer["claude_session_id"] == ""  # baseline

    # Simulate the routes.wrapped() persistence step in isolation.
    # The actual agentcli emits done with session_id "fresh-uuid".
    done_payload = {"type": "done", "text": "hi", "session_id": "fresh-uuid"}
    chunk = (
        f"event: done\ndata: {json.dumps(done_payload, ensure_ascii=False)}\n\n"
    )

    # Replicate the parse-and-persist step that routes.wrapped() must perform.
    new_claude_sid = ""
    if "event: done" in chunk:
        data_line = chunk.split("data: ", 1)[1].split("\n")[0]
        done = json.loads(data_line)
        new_claude_sid = done.get("session_id", "")

    if new_claude_sid:
        mgr.set_claude_session_id(outer_id, new_claude_sid)

    reloaded = mgr.get(outer_id)
    assert reloaded["claude_session_id"] == "fresh-uuid", \
        "agentcli UUID must be persisted onto outer session for resume to work"


@pytest.mark.asyncio
async def test_routes_chat_stream_persists_agentcli_session_id(session_dir, monkeypatch):
    """End-to-end: /api/chat/stream call must save agentcli UUID after stream.

    Mocks stream_claude_chat to yield a done chunk with session_id and verifies
    SessionManager.set_claude_session_id is called.
    """
    from src.web import routes
    # Reload SessionManager singleton bound to our patched SESSIONS_DIR
    import src.session as sess_mod
    monkeypatch.setattr(routes, "_session_mgr", sess_mod.SessionManager())

    captured = {"saved": None}
    orig_set = routes._session_mgr.set_claude_session_id
    def _spy(session_id, claude_session_id):
        captured["saved"] = (session_id, claude_session_id)
        return orig_set(session_id, claude_session_id)
    monkeypatch.setattr(routes._session_mgr, "set_claude_session_id", _spy)

    # Stub stream_claude_chat to emit a done chunk with a fresh session_id
    async def fake_generate():
        yield 'event: text\ndata: {"delta":"hi"}\n\n'
        yield (
            'event: done\ndata: '
            + json.dumps({"text": "hi", "session_id": "uuid-from-agentcli",
                          "usage": {"input_tokens": 1, "output_tokens": 1, "cached_tokens": 0}})
            + '\n\n'
        )

    async def fake_stream_claude_chat(prompt, session_id, model):
        # Return generator factory and the (stale) used_sid as before
        return fake_generate, session_id

    monkeypatch.setattr(routes, "stream_claude_chat", fake_stream_claude_chat)
    monkeypatch.setattr(routes.broker, "publish", AsyncMock())

    # Call the endpoint handler — exhaust the streaming response body
    resp = await routes.chat_stream(
        request=MagicMock(), message="hi", provider="claude", model="", session_id="",
    )
    # Drain the StreamingResponse
    async for _ in resp.body_iterator:
        pass

    assert captured["saved"] is not None, "set_claude_session_id must be called"
    outer_sid, claude_uuid = captured["saved"]
    assert claude_uuid == "uuid-from-agentcli"
    # Outer session JSON should now carry the UUID
    reloaded = routes._session_mgr.get(outer_sid)
    assert reloaded["claude_session_id"] == "uuid-from-agentcli"

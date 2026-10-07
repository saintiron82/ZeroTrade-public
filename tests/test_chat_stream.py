"""Tests for chat SSE stream session_id propagation.

The frontend (`src/web/static/index.html`) persists session_id from a separate
`session` SSE event. Without that event, every chat request sends an empty
session_id back, and agentcli starts a brand-new conversation every turn —
breaking history. This test guards that the backend always emits a `session`
event as the last event in the stream, regardless of success/failure.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import pytest


@pytest.fixture
def chat_stream():
    import importlib
    import src.web.chat_stream as cs
    importlib.reload(cs)
    return cs


async def _consume(generate):
    out = []
    async for ev in generate():
        out.append(ev)
    return out


def _make_chunk(**kw):
    return SimpleNamespace(
        type=kw.get("type", "text"),
        content=kw.get("content", ""),
        data=kw.get("data"),
        usage=kw.get("usage"),
        session_id=kw.get("session_id", ""),
    )


def _events_of_type(raw_events: list[str], event_name: str) -> list[str]:
    return [e for e in raw_events if e.startswith(f"event: {event_name}\n")]


@pytest.mark.asyncio
async def test_session_event_emitted_on_success(chat_stream):
    """Successful turn: session event must be the LAST event with the resolved sid."""
    async def fake_stream(*args, **kwargs):
        for c in (
            _make_chunk(type="text", content="안녕"),
            _make_chunk(type="done", content="안녕", session_id="sess-xyz",
                        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                                              cached_tokens=0)),
        ):
            yield c

    client = SimpleNamespace(chat_stream=fake_stream)
    with patch.object(chat_stream, "_get_client", return_value=client):
        generate, _ = await chat_stream.stream_claude_chat(
            "hi", session_id="", model="")
        events = await _consume(generate)

    session_events = _events_of_type(events, "session")
    assert len(session_events) == 1, f"expected 1 session event, got {len(session_events)}"
    assert "sess-xyz" in session_events[0]
    # Must be the LAST event so frontend's close() doesn't race
    assert events[-1].startswith("event: session\n")


@pytest.mark.asyncio
async def test_session_event_emitted_on_exception(chat_stream):
    """Mid-stream exception still emits session event (preserves prior session_id)."""
    async def fake_stream(*args, **kwargs):
        yield _make_chunk(type="text", content="시작")
        raise RuntimeError("provider crashed")

    client = SimpleNamespace(chat_stream=fake_stream)
    with patch.object(chat_stream, "_get_client", return_value=client):
        generate, _ = await chat_stream.stream_claude_chat(
            "hi", session_id="sess-prev", model="")
        events = await _consume(generate)

    # error event present
    assert _events_of_type(events, "error")
    # session event still present with the original session_id preserved
    session_events = _events_of_type(events, "session")
    assert len(session_events) == 1
    assert "sess-prev" in session_events[0]


@pytest.mark.asyncio
async def test_session_id_resolved_from_done_chunk_when_missing(chat_stream):
    """No incoming session_id, agentcli assigns one via done.session_id → propagated."""
    async def fake_stream(*args, **kwargs):
        yield _make_chunk(type="done", content="ok", session_id="freshly-minted",
                          usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                                cached_tokens=0))

    client = SimpleNamespace(chat_stream=fake_stream)
    with patch.object(chat_stream, "_get_client", return_value=client):
        generate, _ = await chat_stream.stream_claude_chat(
            "hi", session_id="", model="")
        events = await _consume(generate)

    session_events = _events_of_type(events, "session")
    assert len(session_events) == 1
    assert "freshly-minted" in session_events[0]

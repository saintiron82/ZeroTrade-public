from types import SimpleNamespace
from unittest.mock import patch


def _ai_caller():
    """호출 시점에 현재 sys.modules의 모듈을 가져온다.

    test_ai_caller_bridge가 src.bots.ai_caller를 sys.modules에서 제거·재임포트하므로,
    모듈 레벨 import는 stale 참조가 되어 patch가 적용되지 않는다.
    """
    import src.bots.ai_caller as ai_caller
    return ai_caller


def test_invoke_ai_result_returns_content_and_metadata():
    fake_response = SimpleNamespace(
        content="hello",
        provider="codex",
        model="gpt-5",
        error="",
        error_type="",
        tokens=SimpleNamespace(total_tokens=321),
    )

    with (
        patch("src.bots.ai_caller._get_llm_client") as get_client,
        patch("src.bots.ai_caller._log_token_usage") as log_token_usage,
    ):
        get_client.return_value.chat.return_value = fake_response
        result = _ai_caller().invoke_ai_result(
            "prompt",
            {"ai_provider": "codex", "ai_model": "gpt-5"},
            timeout=3,
            bot_id="mirinae-4",
        )

    assert result.content == "hello"
    assert result.provider == "codex"
    assert result.model == "gpt-5"
    assert result.tokens == 321
    assert result.error == ""
    assert result.error_type == ""
    assert result.latency_ms >= 0
    get_client.return_value.chat.assert_called_once_with(
        "prompt",
        provider="codex",
        model="gpt-5",
        owner="mirinae-4",
        conversation_id="bot:mirinae-4",
        alias="",
        timeout=3,
        cwd=None,
        fallback=True,
    )
    log_token_usage.assert_called_once_with(
        "mirinae-4", "codex", "gpt-5", 321, len("prompt"), len("hello")
    )


def test_invoke_ai_result_forwards_conversation_and_alias():
    fake_response = SimpleNamespace(
        content="with context",
        provider="codex",
        model="gpt-5",
        tokens=SimpleNamespace(total_tokens=0),
    )

    with patch("src.bots.ai_caller._get_llm_client") as get_client:
        get_client.return_value.chat.return_value = fake_response
        result = _ai_caller().invoke_ai_result(
            "prompt",
            {"ai_provider": "codex", "ai_model": "gpt-5"},
            timeout=7,
            bot_id="mirinae-4",
            conversation_id="conv-123",
            alias="reviewer",
            cwd="/tmp/unused",
        )

    assert result.content == "with context"
    get_client.return_value.chat.assert_called_once_with(
        "prompt",
        provider="codex",
        model="gpt-5",
        owner="mirinae-4",
        conversation_id="conv-123",
        alias="reviewer",
        timeout=7,
        cwd="/tmp/unused",
        fallback=True,
    )


def test_invoke_ai_returns_plain_string_with_compatibility_params():
    fake_response = SimpleNamespace(
        content="plain text",
        provider="claude",
        model="sonnet",
        tokens=SimpleNamespace(total_tokens=0),
    )

    with patch("src.bots.ai_caller._get_llm_client") as get_client:
        get_client.return_value.chat.return_value = fake_response
        result = _ai_caller().invoke_ai(
            "prompt",
            {"ai_provider": "claude", "ai_model": "sonnet"},
            timeout=5,
            bot_id="evolver",
            conversation_id="conv-plain",
            alias="runner",
            cwd="/tmp/unused",
        )

    assert result == "plain text"
    get_client.return_value.chat.assert_called_once_with(
        "prompt",
        provider="claude",
        model="sonnet",
        owner="evolver",
        conversation_id="conv-plain",
        alias="runner",
        timeout=5,
        cwd="/tmp/unused",
        fallback=True,
    )


def test_invoke_ai_result_preserves_error_metadata_when_response_is_empty():
    fake_response = SimpleNamespace(
        content="",
        provider="codex",
        model="gpt-5",
        error="timeout",
        error_type="TimeoutError",
        tokens=SimpleNamespace(total_tokens=0),
    )

    with patch("src.bots.ai_caller._get_llm_client") as get_client:
        get_client.return_value.chat.return_value = fake_response
        result = _ai_caller().invoke_ai_result(
            "prompt",
            {"ai_provider": "codex", "ai_model": "gpt-5"},
            timeout=1,
            bot_id="mirinae-4",
        )

    assert result.content == ""
    assert result.provider == "codex"
    assert result.model == "gpt-5"
    assert result.tokens == 0
    assert result.error == "timeout"
    assert result.error_type == "TimeoutError"

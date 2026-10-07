"""Bridge test: src.bots.ai_caller이 LLMClient를 통해 동작하는지 검증."""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock


def _stub_src_deps():
    """dotenv/pydantic 등 외부 패키지가 없는 환경에서 src 임포트 허용."""
    stubs = ["dotenv", "pydantic", "pydantic_settings"]
    for mod in stubs:
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()

    # src.config를 직접 stub — PROJECT_ROOT만 노출하면 됨
    if "src.config" not in sys.modules:
        config_mock = MagicMock()
        config_mock.PROJECT_ROOT = Path(__file__).resolve().parents[3]
        sys.modules["src.config"] = config_mock

    # ai_caller이 이미 캐시되었다면 제거해서 재임포트
    for key in list(sys.modules):
        if key == "src.bots.ai_caller":
            del sys.modules[key]


_CODEX_STDOUT = (
    '{"type":"thread.started","thread_id":"bridge-sid"}\n'
    '{"type":"item.completed","item":{"type":"agent_message","text":"결과"}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}\n'
)


@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_ai_bridge(mock_env, mock_run):
    """기존 invoke_ai 인터페이스가 LLMClient 브릿지를 통해 동작."""
    _stub_src_deps()
    mock_run.return_value = MagicMock(returncode=0, stdout=_CODEX_STDOUT, stderr="")

    import src.bots.ai_caller as ai_caller
    ai_caller._llm_client = None

    result = ai_caller.invoke_ai(
        "분석해줘", {"ai_provider": "codex", "ai_model": ""}, bot_id="test")
    assert result == "결과"


@patch("agentcli.providers.codex.subprocess.run")
@patch("agentcli.providers.codex.build_env", return_value={"PATH": "/usr/bin"})
def test_invoke_ai_returns_string(mock_env, mock_run):
    """반환값이 여전히 문자열인지 확인 (기존 호환)."""
    _stub_src_deps()
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout='{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}',
        stderr="")

    import src.bots.ai_caller as ai_caller
    ai_caller._llm_client = None

    result = ai_caller.invoke_ai("hi", {"ai_provider": "codex"})
    assert isinstance(result, str)


def test_invoke_ai_enables_provider_fallback_by_default():
    _stub_src_deps()
    import src.bots.ai_caller as ai_caller

    fake_client = MagicMock()
    fake_client.chat.return_value = MagicMock(
        content="ok",
        provider="claude",
        model="",
        tokens=MagicMock(total_tokens=0),
    )

    with patch.object(ai_caller, "_get_llm_client", return_value=fake_client):
        result = ai_caller.invoke_ai("hi", {"ai_provider": "codex"}, bot_id="mirinae-2")

    assert result == "ok"
    assert fake_client.chat.call_args.kwargs["fallback"] is True


def test_invoke_ai_drops_claude_model_for_codex_provider():
    _stub_src_deps()
    import src.bots.ai_caller as ai_caller

    fake_client = MagicMock()
    fake_client.chat.return_value = MagicMock(
        content="ok",
        provider="codex",
        model="",
        tokens=MagicMock(total_tokens=0),
    )

    with patch.object(ai_caller, "_get_llm_client", return_value=fake_client):
        ai_caller.invoke_ai(
            "hi",
            {"ai_provider": "codex", "ai_model": "sonnet"},
            bot_id="mirinae-2",
        )

    assert fake_client.chat.call_args.kwargs["model"] == ""

"""FF_MCP_READONLY=1이면 쓰기성 MCP 도구가 등록되지 않는다."""

import importlib
import sys

import pytest

# 시스템 상태를 바꾸는 도구 전수 (주문/생성/제어/설정변경)
WRITE_TOOLS = {
    # 주문
    "buy_stock", "sell_stock", "buy_us_stock", "sell_us_stock",
    # 파이프 생성/제어/예산
    "create_strategy", "update_pipe", "start_pipe", "stop_pipe",
    "link_bot", "set_bot_ratio", "set_total_budget",
    # 봇 생성/파라미터 변경
    "create_bot", "design_bot", "save_bot_config", "run_evolver",
}
READ_TOOLS_SAMPLE = {"get_price", "get_balance", "get_daily_chart", "search_stock"}


@pytest.fixture(autouse=True)
def _isolate_mcp_module():
    """테스트 전후 src.mcp_server 모듈 캐시를 비워 다른 테스트와 격리한다."""
    sys.modules.pop("src.mcp_server", None)
    yield
    sys.modules.pop("src.mcp_server", None)


def _load_mcp_tools(monkeypatch, readonly):
    if readonly is None:
        monkeypatch.delenv("FF_MCP_READONLY", raising=False)
    else:
        monkeypatch.setenv("FF_MCP_READONLY", readonly)
    sys.modules.pop("src.mcp_server", None)
    mod = importlib.import_module("src.mcp_server")
    return set(mod.mcp._tool_manager._tools.keys()), mod


def test_readonly_mode_excludes_write_tools(monkeypatch):
    tools, _ = _load_mcp_tools(monkeypatch, "1")
    assert WRITE_TOOLS & tools == set(), f"readonly인데 등록됨: {WRITE_TOOLS & tools}"
    assert READ_TOOLS_SAMPLE <= tools


def test_default_mode_includes_write_tools(monkeypatch):
    tools, _ = _load_mcp_tools(monkeypatch, None)
    assert WRITE_TOOLS <= tools
    assert READ_TOOLS_SAMPLE <= tools


@pytest.mark.parametrize("value", ["true", "yes", "1", "TRUE", " on "])
def test_fail_closed_any_set_value_is_readonly(monkeypatch, value):
    """fail-closed: 변수가 설정되어 있기만 하면(명시적 off 제외) 읽기 전용."""
    tools, _ = _load_mcp_tools(monkeypatch, value)
    assert WRITE_TOOLS & tools == set(), f"{value!r}인데 등록됨: {WRITE_TOOLS & tools}"
    assert READ_TOOLS_SAMPLE <= tools


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off", "False"])
def test_explicit_off_values_keep_full_mode(monkeypatch, value):
    tools, _ = _load_mcp_tools(monkeypatch, value)
    assert WRITE_TOOLS <= tools
    assert READ_TOOLS_SAMPLE <= tools

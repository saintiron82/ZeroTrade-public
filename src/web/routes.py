"""FastAPI 라우터 — API 엔드포인트."""

import asyncio
import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import src.trading_mode as tm
from src.ai.analyzer import AIAnalyzer, AIProvider
from src.ai.parser import parse_ai_response, save_conditions
from src.ai.prompt import build_analysis_prompt
from src.config import settings
from src.trade.logger import TradeLogger
from src.web.chat_stream import stream_claude_chat
from src.web.sse import broker

logger = logging.getLogger(__name__)

from src.bot_manager import get_bot_manager

router = APIRouter(prefix="/api")


def _pipe_runtime_map() -> dict[str, dict]:
    """Return live runner state keyed by pipe id."""
    try:
        from src.services.orchestrator import Orchestrator
        services = Orchestrator().status()
    except Exception as e:
        logger.debug("파이프 런타임 상태 조회 실패: %s", e)
        return {}

    runtime = {}
    for service in services:
        if service.get("type") == "pipe" and service.get("id"):
            runtime[service["id"]] = {
                "runner_alive": bool(service.get("alive")),
                "runner_pid": service.get("pid") if service.get("alive") else 0,
            }
    return runtime


def _attach_pipe_runtime(summary: dict, runtime: dict[str, dict]) -> dict:
    pipe_id = summary.get("id", "")
    status = runtime.get(pipe_id, {"runner_alive": False, "runner_pid": 0})
    summary["configured_active"] = bool(summary.get("active"))
    summary["runner_alive"] = bool(status.get("runner_alive"))
    summary["runner_pid"] = status.get("runner_pid") or 0
    return summary


class AnalyzeRequest(BaseModel):
    stocks: list[str] = []
    rules: str = ""
    provider: str = "claude"
    model: str = ""


class ChatRequest(BaseModel):
    message: str
    provider: str = "claude"
    model: str = ""
    session_id: str = ""


# ── 봇 상태 ──

@router.get("/status")
async def get_status():
    mgr = get_bot_manager()
    return mgr.status()


# ── 봇 제어 ──

@router.post("/bot/start")
async def start_bot():
    """BotManager 시작 (활성 전략의 WebSocket 연결)."""
    mgr = get_bot_manager()
    if mgr._running:
        return {"status": "already_running"}
    await mgr.start()
    await broker.publish("system", {"message": "봇 매니저 시작"})
    return {"status": "started"}


@router.post("/bot/stop")
async def stop_bot():
    """BotManager 전체 정지."""
    mgr = get_bot_manager()
    await mgr.stop()
    await broker.publish("system", {"message": "봇 매니저 정지"})
    return {"status": "stopped"}


@router.post("/strategies/{strategy_id}/activate")
async def activate_strategy(strategy_id: str):
    """전략 봇 활성화 — 조건식 로드 + 종목 감시 시작."""
    mgr = get_bot_manager()
    ok, msg = mgr.activate(strategy_id)
    if ok:
        if not mgr._running:
            await mgr.start()
        await broker.publish("system", {"message": msg})
    return {"success": ok, "message": msg}


@router.post("/strategies/{strategy_id}/deactivate")
async def deactivate_strategy(strategy_id: str):
    """전략 봇 비활성화."""
    mgr = get_bot_manager()
    ok, msg = mgr.deactivate(strategy_id)
    if ok:
        await broker.publish("system", {"message": msg})
    return {"success": ok, "message": msg}


# ── 봇 레지스트리 ──

def _to_float(value, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _position_minutes(pos: dict) -> int:
    from datetime import datetime

    raw = pos.get("bought_at") or pos.get("entry_time") or ""
    if not raw:
        return int(_to_float(pos.get("held_min"), 0))
    try:
        start = datetime.fromisoformat(str(raw))
        now = datetime.now(start.tzinfo) if start.tzinfo else datetime.now()
        return max(0, int((now - start).total_seconds() // 60))
    except (TypeError, ValueError):
        return int(_to_float(pos.get("held_min"), 0))


def _refresh_position_prices(positions: list[dict]) -> list[dict]:
    """Attach current market prices to saved positions when live status is absent/stale."""
    if not positions:
        return positions
    try:
        from src.kis.market import KISMarket
        market = KISMarket()
    except Exception:
        return positions

    refreshed = []
    for pos in positions:
        item = dict(pos)
        code = item.get("code", "")
        qty = int(_to_float(item.get("qty"), 0))
        entry = _to_float(item.get("entry_price"), 0)
        try:
            info = market.get_price(code) if code else None
            current = _to_float((info or {}).get("price"), 0)
        except Exception:
            current = 0
        if current > 0:
            item["current_price"] = int(current) if current.is_integer() else current
            item["unrealized"] = int((current - entry) * qty) if entry and qty else 0
            item["pnl_pct"] = round((current - entry) / entry * 100, 2) if entry else 0
            item["price_source"] = "market"
        refreshed.append(item)
    return refreshed


def _normalize_positions(status: dict, db_state: dict | None = None) -> list[dict]:
    db_state = db_state or {}
    raw_positions = status.get("positions") or db_state.get("positions") or []
    positions = []
    for pos in raw_positions:
        code = pos.get("code") or pos.get("symbol") or ""
        qty = int(_to_float(pos.get("qty") or pos.get("quantity"), 0))
        entry = _to_float(pos.get("entry_price", pos.get("price", pos.get("avg_price", 0))), 0)
        current = _to_float(pos.get("current_price", 0), 0) or entry
        target = _to_float(pos.get("target_price"), 0)
        stop = _to_float(pos.get("stop_loss"), 0)
        unrealized = _to_float(pos.get("unrealized"), (current - entry) * qty if entry and current else 0)
        pnl_pct = _to_float(pos.get("pnl_pct"), ((current - entry) / entry * 100) if entry else 0)
        if not code or qty <= 0:
            continue
        positions.append({
            **pos,
            "code": code,
            "name": pos.get("name", ""),
            "qty": qty,
            "entry_price": int(entry) if entry.is_integer() else entry,
            "current_price": int(current) if current.is_integer() else current,
            "target_price": int(target) if target.is_integer() else target,
            "stop_loss": int(stop) if stop.is_integer() else stop,
            "unrealized": int(unrealized) if unrealized.is_integer() else unrealized,
            "pnl_pct": round(pnl_pct, 2),
            "held_min": _position_minutes(pos),
        })

    if positions:
        return positions

    holding = status.get("holding") or {}
    code = holding.get("code") or db_state.get("holding_code", "")
    qty = int(_to_float(holding.get("qty", db_state.get("holding_qty", 0)), 0))
    price = _to_float(holding.get("price", db_state.get("holding_price", 0)), 0)
    if code and qty > 0:
        return [{
            "code": code,
            "name": holding.get("name", ""),
            "qty": qty,
            "entry_price": int(price) if price.is_integer() else price,
            "current_price": int(price) if price.is_integer() else price,
            "target_price": 0,
            "stop_loss": 0,
            "unrealized": 0,
            "pnl_pct": 0,
            "held_min": 0,
        }]
    return []


def _split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?。])\s+|(?<=다\.)\s+|(?<=요\.)\s+|(?<=\))\s+", text)
    return [p.strip() for p in parts if p and p.strip()]


def _position_mentions(sentence: str, code: str, name: str) -> bool:
    if not sentence:
        return False
    if code and code in sentence:
        return True
    if name and name in sentence:
        return True
    return False


def _extract_position_rationale(code: str, name: str, sources: list[str], allow_hold_sentence: bool = False) -> str:
    picked = []
    for text in sources:
        for sentence in _split_sentences(text):
            if _position_mentions(sentence, code, name):
                picked.append(sentence)
            elif allow_hold_sentence and "보유" in sentence and any(word in sentence for word in ("유지", "HOLD", "손절", "목표")):
                picked.append(sentence)
            if len(picked) >= 3:
                break
        if len(picked) >= 3:
            break
    return " ".join(dict.fromkeys(picked))


def _report_plan_summary(report: dict) -> str:
    current = _to_float(report.get("current_price"), 0)
    target = _to_float(report.get("target_price"), 0)
    stop = _to_float(report.get("stop_loss"), 0)
    parts = []
    if target:
        gap = ((target - current) / current * 100) if current else 0
        parts.append(f"목표 {target:,.0f}원까지 {gap:+.2f}%")
    if stop:
        gap = ((current - stop) / current * 100) if current else 0
        parts.append(f"손절 {stop:,.0f}원까지 {gap:.2f}% 여유")
    return ", ".join(parts) if parts else "목표가/손절가 미설정"


def _report_sell_trigger(report: dict) -> str:
    target = _to_float(report.get("target_price"), 0)
    stop = _to_float(report.get("stop_loss"), 0)
    triggers = []
    if stop:
        triggers.append(f"현재가가 {stop:,.0f}원 이하로 밀리면 손절/축소 검토")
    if target:
        triggers.append(f"{target:,.0f}원 도달 시 익절 또는 목표 상향 재평가")
    return "; ".join(triggers) if triggers else "명확한 매도/재평가 조건이 아직 기록되지 않았습니다."


def _report_plan_validation(current: float, target: float, stop: float, risk_reward: float | None) -> dict:
    min_rr = 1.2
    if current <= 0:
        return {"plan_valid": False, "plan_status": "unpriced", "plan_warning": "현재가 조회 실패로 목표/손절 검증 불가"}
    if target <= 0 or stop <= 0:
        return {"plan_valid": False, "plan_status": "missing", "plan_warning": "목표가/손절가 미설정"}
    if stop >= current:
        return {"plan_valid": False, "plan_status": "invalid_stop", "plan_warning": f"손절가가 현재가 이상({stop:,.0f} >= {current:,.0f})"}
    if target <= current:
        return {"plan_valid": False, "plan_status": "target_reached", "plan_warning": f"목표가 도달/초과({target:,.0f} <= {current:,.0f})"}
    if risk_reward is not None and risk_reward < min_rr:
        return {"plan_valid": False, "plan_status": "poor_risk_reward", "plan_warning": f"손익비 부족({risk_reward:.2f}:1 < {min_rr:.2f}:1)"}
    return {"plan_valid": True, "plan_status": "ok", "plan_warning": ""}


def _build_position_reports(bot_id: str, status: dict, db_state: dict | None = None) -> list[dict]:
    """Build per-position report cards for UI consumers."""
    db_state = db_state or {}
    positions = _refresh_position_prices(_normalize_positions(status, db_state))
    last_analysis = status.get("last_analysis") or db_state.get("last_analysis") or {}
    decision = last_analysis.get("decision") if isinstance(last_analysis, dict) else {}
    decision = decision or {}
    reviews = (
        status.get("position_reviews")
        or db_state.get("position_reviews")
        or (last_analysis.get("position_reviews") if isinstance(last_analysis, dict) else [])
        or []
    )
    debate_log = status.get("debate_log") or db_state.get("debate_log") or []

    reports = []
    for pos in positions:
        code = pos["code"]
        review = next((r for r in reviews if r.get("code") == code or r.get("symbol") == code), {})
        decision_for_code = decision if decision.get("code") in ("", code, None) else {}
        current = _to_float(pos.get("current_price"), 0)
        entry = _to_float(pos.get("entry_price"), 0)
        decision_target = _to_float(decision_for_code.get("target_price"), 0)
        decision_stop = _to_float(decision_for_code.get("stop_loss"), 0)
        decision_target_action = str(decision_for_code.get("target_action") or "").lower()
        use_decision_plan = (
            decision_target > 0
            and decision_stop > 0
            and decision_target_action in ("raise", "lower", "keep")
        )
        if use_decision_plan:
            target = decision_target
            stop = decision_stop
        else:
            target = (
                _to_float(pos.get("target_price"), 0)
                or _to_float(review.get("target_price"), 0)
                or decision_target
            )
            stop = (
                _to_float(pos.get("stop_loss"), 0)
                or _to_float(review.get("stop_loss"), 0)
                or decision_stop
            )
        upside_pct = ((target - current) / current * 100) if current and target else 0
        stop_gap_pct = ((current - stop) / current * 100) if current and stop else 0
        downside = current - stop if current and stop else 0
        upside = target - current if current and target else 0
        risk_reward = round(upside / downside, 2) if upside > 0 and downside > 0 else None

        latest_debate = next((d for d in reversed(debate_log) if d.get("code") == code), {})
        position_rationale = _extract_position_rationale(
            code,
            pos.get("name", ""),
            [
                decision.get("trader_reason", ""),
                decision.get("bull_summary", ""),
                decision.get("bear_summary", ""),
                latest_debate.get("trader", ""),
                latest_debate.get("bull", ""),
                latest_debate.get("bear", ""),
            ],
            allow_hold_sentence=len(positions) == 1,
        )
        thesis = (
            decision_for_code.get("trader_reason")
            or position_rationale
            or latest_debate.get("trader")
            or review.get("reason")
            or "보유 판단 근거가 아직 충분히 기록되지 않았습니다."
        )

        plan_validation = _report_plan_validation(current, target, stop, risk_reward)
        review_plan_valid = review.get("plan_valid", plan_validation["plan_valid"])
        review_plan_status = review.get("plan_status", plan_validation["plan_status"])
        review_plan_warning = review.get("plan_warning", plan_validation["plan_warning"])
        if use_decision_plan:
            review_plan_valid = plan_validation["plan_valid"]
            review_plan_status = plan_validation["plan_status"]
            review_plan_warning = plan_validation["plan_warning"]
        report = {
            "bot_id": bot_id,
            "code": code,
            "name": pos.get("name", ""),
            "qty": pos.get("qty", 0),
            "entry_price": pos.get("entry_price", 0),
            "current_price": pos.get("current_price", 0),
            "target_price": int(target) if target.is_integer() else target,
            "stop_loss": int(stop) if stop.is_integer() else stop,
            "unrealized": pos.get("unrealized", 0),
            "pnl_pct": pos.get("pnl_pct", 0),
            "held_min": pos.get("held_min", 0),
            "target_gap_pct": round(upside_pct, 2),
            "stop_gap_pct": round(stop_gap_pct, 2),
            "risk_reward": risk_reward,
            "plan_valid": review_plan_valid,
            "plan_status": review_plan_status,
            "plan_warning": review_plan_warning,
            "decision": (
                decision_for_code.get("decision")
                if use_decision_plan
                else review.get("decision") or decision_for_code.get("decision") or "hold"
            ),
            "target_action": decision_for_code.get("target_action", ""),
            "confidence": decision_for_code.get("confidence", latest_debate.get("confidence", 0)),
            "review_reason": review.get("reason", ""),
            "thesis": thesis,
            "hold_rationale": position_rationale or thesis,
            "bull_summary": (
                decision_for_code.get("bull_summary")
                or _extract_position_rationale(code, pos.get("name", ""), [decision.get("bull_summary", ""), latest_debate.get("bull", "")])
                or latest_debate.get("bull", "")
            ),
            "bear_summary": (
                decision_for_code.get("bear_summary")
                or _extract_position_rationale(code, pos.get("name", ""), [decision.get("bear_summary", ""), latest_debate.get("bear", "")])
                or latest_debate.get("bear", "")
            ),
            "updated_at": (last_analysis.get("time") if isinstance(last_analysis, dict) else "") or db_state.get("updated_at", ""),
        }
        report["plan_summary"] = _report_plan_summary(report)
        report["sell_trigger"] = _report_sell_trigger(report)
        reports.append(report)
    return reports

@router.get("/bots")
async def list_bots():
    """등록된 모든 봇 메타데이터 조회."""
    from src.bots.registry import list_all
    from dataclasses import asdict
    bots = list_all()
    return {"bots": [asdict(b) for b in bots]}


class CreateBotRequest(BaseModel):
    description: str


@router.post("/bots/create")
async def create_bot_api(req: CreateBotRequest):
    """AI로 봇 생성."""
    import asyncio
    from src.services.bot_builder import BotBuilder
    builder = BotBuilder()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, builder.build, req.description)
    if result.get("success"):
        await broker.publish("system", {"message": f"봇 생성: {result['name']} ({result['bot_id']})"})
    return result


class DesignBotRequest(BaseModel):
    description: str = ""
    preset: str = ""
    overrides: dict = {}


@router.post("/bots/design")
async def design_bot_api(req: DesignBotRequest):
    """AI로 봇 설계 (자연어 → 조건 스펙)."""
    import asyncio
    from src.services.bot_designer import BotDesigner
    designer = BotDesigner()
    loop = asyncio.get_event_loop()
    if req.preset:
        result = designer.design_from_preset(req.preset, req.overrides)
    else:
        result = await loop.run_in_executor(None, designer.design, req.description)
    return result


class SaveBotSpecRequest(BaseModel):
    spec: dict


@router.post("/bots/design/save")
async def save_bot_spec_api(req: SaveBotSpecRequest):
    """봇 스펙 저장 + 레지스트리 등록."""
    from src.services.bot_designer import BotDesigner
    designer = BotDesigner()
    ok, err = designer.validate(req.spec)
    if not ok:
        return {"success": False, "error": err}
    result = designer.save_and_register(req.spec)
    if result.get("success"):
        await broker.publish("system", {"message": f"봇 설계 완료: {req.spec.get('name', '')} ({req.spec.get('bot_id', '')})"})
    return result


@router.get("/bots/design/indicators")
async def get_indicators():
    """사용 가능한 지표 목록."""
    from src.bots.condition_engine import INDICATOR_META
    return {"indicators": INDICATOR_META}


@router.get("/bots/design/presets")
async def get_presets():
    """프리셋 전략 목록."""
    from src.services.bot_designer import PRESETS
    return {"presets": {k: {"name": v["name"], "description": v["description"], "category": v["category"]}
                        for k, v in PRESETS.items()}}


@router.get("/bots/{bot_id}")
async def get_bot_info(bot_id: str):
    """특정 봇 메타데이터 조회."""
    from src.bots.registry import get
    from src.db import get_db
    from dataclasses import asdict
    info = get(bot_id)
    if not info:
        return {"error": f"봇 '{bot_id}' 없음"}
    result = asdict(info)

    db = get_db()
    db_state = db.load_bot_state(bot_id) or {}
    if not db_state:
        row = db._conn.execute(
            "SELECT bot_id FROM bot_state WHERE bot_id LIKE ? ORDER BY updated_at DESC LIMIT 1",
            (f"%:{bot_id}",),
        ).fetchone()
        if row:
            db_state = db.load_bot_state(row["bot_id"]) or {}

    live_status = None
    try:
        mgr = get_bot_manager()
        for name, bot in mgr._polling_bots.items():
            if name == bot_id or name.endswith(f":{bot_id}") or bot_id in name:
                live_status = bot.status()
                break
    except Exception:
        live_status = None

    status = live_status or {
        "running": False,
        "bot_id": bot_id,
        "scan_count": db_state.get("scan_count", 0),
        "holding": {
            "code": db_state.get("holding_code", ""),
            "qty": db_state.get("holding_qty", 0),
            "price": db_state.get("holding_price", 0),
        },
        "positions": db_state.get("positions", []),
        "positions_count": len(db_state.get("positions", []) or []),
        "pnl": {
            "realized": db_state.get("realized_pnl", 0),
            "unrealized": 0,
            "total": db_state.get("realized_pnl", 0),
        },
        "last_analysis": db_state.get("last_analysis", {}),
        "position_reviews": db_state.get("position_reviews", []),
        "debate_log": db_state.get("debate_log", []),
        "recent_trades": db_state.get("recent_trades", []),
    }
    status["position_reports"] = _build_position_reports(bot_id, status, db_state)
    result["status"] = status
    return result


# ── 봇 직접 시작 금지 — 파이프를 통해서만 봇 실행 가능 ──
# 이전: /api/bots/etf-momentum/start 등으로 파이프 없이 봇 직접 시작 가능
# 현재: 모든 봇은 파이프에 연결 후 /api/pipes/{id}/start로만 실행

@router.post("/bots/{bot_id}/start")
async def bot_start_blocked(bot_id: str):
    return {"success": False, "error": f"봇 '{bot_id}'은 직접 시작할 수 없습니다. 파이프에 연결 후 /pipes에서 시작하세요."}

@router.post("/bots/{bot_id}/stop")
async def bot_stop_blocked(bot_id: str):
    return {"success": False, "error": f"봇 '{bot_id}'은 직접 정지할 수 없습니다. /pipes에서 파이프를 정지하세요."}

@router.get("/bots/{bot_id}/status")
async def bot_status_via_pipe(bot_id: str):
    """봇 상태 — DB에서 조회 (파이프 독립)."""
    from src.db import get_db
    db = get_db()
    # bot_id로 시작하는 모든 인스턴스 상태 조회
    states = []
    # 메모리 봇 상태 (BotManager 내부)
    mgr = get_bot_manager()
    for name, bot in mgr._polling_bots.items():
        if bot_id in name:
            states.append({"instance": name, **bot.status()})
    # DB 상태 (독립 프로세스)
    db_state = db.load_bot_state(bot_id)
    if db_state:
        states.append({"instance": bot_id, "source": "db", **db_state})
    return {"bot_id": bot_id, "instances": states}


# ── 파이프 ──

class SetBudgetRequest(BaseModel):
    total_budget: int


@router.post("/pipes/budget")
async def set_total_budget_api(req: SetBudgetRequest):
    from src.strategy import StrategyManager
    mgr = StrategyManager()
    mgr.set_total_budget(req.total_budget)
    return {"success": True, "total_budget": req.total_budget}


class CreatePipeRequest(BaseModel):
    name: str
    budget: int
    bot_id: str = ""
    bot_params: dict = {}
    description: str = ""


@router.post("/pipes")
async def create_pipe(req: CreatePipeRequest):
    from src.strategy import StrategyManager
    mgr = StrategyManager()
    try:
        s = mgr.create_strategy(name=req.name, budget=req.budget, description=req.description)
        if req.bot_id:
            mgr.link_bot(s.id, req.bot_id, req.bot_params)
        await broker.publish("system", {"message": f"파이프 '{s.name}' 생성: {s.budget:,}원"})
        return {"success": True, "pipe_id": s.id, "name": s.name, "budget": s.budget}
    except ValueError as e:
        return {"success": False, "error": str(e)}


@router.get("/pipes")
async def list_pipes():
    from src.strategy import StrategyManager
    mgr = StrategyManager()
    runtime = _pipe_runtime_map()
    pipes = []
    for s in mgr.list_strategies():
        pipes.append(_attach_pipe_runtime(mgr.pipe_summary(s.id), runtime))
    return {
        "total_budget": mgr.portfolio.total_budget,
        "allocated": mgr.portfolio.allocated(),
        "unallocated": mgr.portfolio.unallocated(),
        "pipes": pipes,
    }


@router.get("/pipes/{pipe_id}")
async def get_pipe(pipe_id: str):
    from src.strategy import StrategyManager
    mgr = StrategyManager()
    return _attach_pipe_runtime(mgr.pipe_summary(pipe_id), _pipe_runtime_map())


@router.post("/pipes/{pipe_id}/start")
async def start_pipe(pipe_id: str):
    """파이프 시작 — 독립 프로세스 or 내부 BotManager."""
    from src.strategy import StrategyManager
    mgr_s = StrategyManager()
    s = mgr_s.get(pipe_id)
    if not s:
        return {"success": False, "error": "파이프 없음"}
    if not s.bot_id:
        return {"success": False, "error": "봇이 연결되지 않음"}

    # 독립 프로세스로 시작 시도
    try:
        from src.services.orchestrator import Orchestrator
        orch = Orchestrator()
        pid = orch.start_pipe(pipe_id)
        if pid:
            mgr_s.update_strategy(pipe_id, active=True)
            await broker.publish("system", {"message": f"파이프 '{s.name}' 시작 (PID: {pid})"})
            return {"success": True, "message": f"파이프 시작 (PID: {pid})", "pid": pid}
    except Exception as e:
        logger.warning("독립 프로세스 시작 실패, 내부 실행: %s", e)

    # fallback: 내부 BotManager — 모든 봇 시작
    from src.bots.factory import create_bot
    mgr_b = get_bot_manager()
    bot_entries = s.active_bots()
    started = []
    failed = []

    for entry in bot_entries:
        bot_id = entry["bot_id"]
        ratio = entry.get("ratio", 1.0)
        bot_budget = int(s.budget * ratio)
        instance_id = f"{pipe_id}:{bot_id}"
        params = entry.get("params", {})

        bot = create_bot(bot_id, budget=bot_budget, instance_id=instance_id, params=params)
        if bot:
            mgr_b._polling_bots[instance_id] = bot
            await bot.start()
            started.append(f"{bot_id}({int(ratio*100)}%={bot_budget:,}원)")
        else:
            failed.append(bot_id)

    if started:
        mgr_s.update_strategy(pipe_id, active=True)
        msg = f"파이프 '{s.name}' 시작 — {', '.join(started)}"
        await broker.publish("system", {"message": msg})
        return {"success": True, "message": msg}
    return {"success": False, "message": f"봇 시작 실패: {', '.join(failed)}"}


@router.post("/pipes/{pipe_id}/stop")
async def stop_pipe(pipe_id: str):
    """파이프 정지 — 독립 프로세스 or 내부 BotManager."""
    from src.strategy import StrategyManager
    mgr_s = StrategyManager()
    s = mgr_s.get(pipe_id)
    if not s:
        return {"success": False, "error": "파이프 없음"}

    # 독립 프로세스 정지 시도
    try:
        from src.services.orchestrator import Orchestrator
        Orchestrator().stop_service("pipe", pipe_id)
    except Exception:
        pass

    # 내부 BotManager — 이 파이프에 속한 모든 봇 정지
    mgr_b = get_bot_manager()
    stopped = []
    for entry in s.active_bots():
        instance_id = f"{pipe_id}:{entry['bot_id']}"
        bot = mgr_b._polling_bots.pop(instance_id, None)
        if bot:
            await bot.stop()
            stopped.append(entry["bot_id"])

    # 레거시 호환: 단일 봇 키로도 정지 시도
    if not stopped and s.bot_id:
        for key in list(mgr_b._polling_bots.keys()):
            if s.bot_id in key or key == s.bot_id:
                bot = mgr_b._polling_bots.pop(key, None)
                if bot:
                    await bot.stop()
                    stopped.append(key)

    mgr_s.update_strategy(pipe_id, active=False)
    await broker.publish("system", {"message": f"파이프 '{s.name}' 정지"})
    return {"success": True, "message": f"파이프 정지 ({len(stopped)}개 봇 종료)"}


class LinkBotRequest(BaseModel):
    bot_id: str
    ratio: float = 0  # 0이면 자동 재분배


@router.post("/pipes/{pipe_id}/link-bot")
async def link_bot_to_pipe(pipe_id: str, req: LinkBotRequest):
    """파이프에 봇 투입. ratio=0이면 기존 봇과 균등 재분배."""
    from src.strategy import StrategyManager
    mgr = StrategyManager()
    s = mgr.get(pipe_id)
    if not s:
        return {"success": False, "error": "파이프 없음"}
    try:
        auto_rebalance = (req.ratio <= 0)
        if not s.bots and req.ratio <= 0:
            # 첫 봇: 100%
            mgr.link_bot(pipe_id, req.bot_id)
        else:
            mgr.add_bot(pipe_id, req.bot_id, ratio=req.ratio, auto_rebalance=auto_rebalance)
        s = mgr.get(pipe_id)  # 재분배 후 갱신
        bots_summary = ", ".join(f"{b['bot_id']}({int(b.get('ratio',0)*100)}%)" for b in s.bots)
        await broker.publish("system", {"message": f"파이프 '{s.name}'에 봇 '{req.bot_id}' 투입 → {bots_summary}"})
        return {"success": True, "bots": s.active_bots()}
    except ValueError as e:
        return {"success": False, "error": str(e)}

class SetBotRatioRequest(BaseModel):
    bot_id: str
    ratio: float


@router.put("/pipes/{pipe_id}/bot-ratio")
async def set_bot_ratio(pipe_id: str, req: SetBotRatioRequest):
    """봇 비율 변경 + 나머지 봇 자동 재분배."""
    from src.strategy import StrategyManager
    mgr = StrategyManager()
    try:
        mgr.set_bot_ratio_rebalance(pipe_id, req.bot_id, req.ratio)
        s = mgr.get(pipe_id)
        return {"success": True, "bots": [{"bot_id": b.get("bot_id"), "ratio": b.get("ratio")} for b in s.bots]}
    except ValueError as e:
        return {"success": False, "error": str(e)}


class SetBotIntervalRequest(BaseModel):
    bot_id: str
    interval_seconds: int


@router.put("/pipes/{pipe_id}/bot-interval")
async def set_bot_interval(pipe_id: str, req: SetBotIntervalRequest):
    """봇 인스턴스의 분석 주기 변경 + 실행 중이면 라이브 반영.

    registry 범위로 검증 → strategies.json 저장 → Redis 명령 발행.
    """
    from src.strategy import StrategyManager
    from src.bots import registry
    from src.services.redis_bus import get_bus

    spec = registry.interval_spec(req.bot_id)
    if not spec:
        return {"success": False, "error": f"{req.bot_id}는 주기 조절을 지원하지 않습니다"}
    if not (spec["min"] <= req.interval_seconds <= spec["max"]):
        return {"success": False,
                "error": f"주기는 {spec['min']}~{spec['max']}초 범위여야 합니다"}

    mgr = StrategyManager()
    if not mgr.set_bot_interval(pipe_id, req.bot_id, req.interval_seconds):
        return {"success": False, "error": "파이프에 해당 봇이 없습니다"}

    try:
        get_bus().send_command("pipe", pipe_id, "set_bot_interval", {
            "bot_id": req.bot_id,
            "interval_seconds": req.interval_seconds,
        })
    except Exception as e:
        # 저장은 됐으므로 다음 가동 시 적용. 명령 실패는 치명적이지 않음.
        logger.warning("set_bot_interval Redis 명령 실패: %s", e)

    return {"success": True, "interval_seconds": req.interval_seconds}


class SetBotRatiosRequest(BaseModel):
    ratios: dict  # {bot_id: ratio}


@router.put("/pipes/{pipe_id}/bot-ratios")
async def set_bot_ratios_bulk(pipe_id: str, req: SetBotRatiosRequest):
    """봇 비율 일괄 설정."""
    from src.strategy import StrategyManager
    mgr = StrategyManager()
    try:
        mgr.set_bot_ratios_bulk(pipe_id, req.ratios)
        return {"success": True}
    except ValueError as e:
        return {"success": False, "error": str(e)}


@router.delete("/pipes/{pipe_id}/bots/{bot_id}")
async def remove_bot_from_pipe(pipe_id: str, bot_id: str):
    """파이프에서 봇 제거."""
    from src.strategy import StrategyManager
    mgr = StrategyManager()
    mgr.remove_bot(pipe_id, bot_id)
    return {"success": True}


@router.post("/pipes/{pipe_id}/bots/{bot_id}/toggle")
async def toggle_bot_in_pipe(pipe_id: str, bot_id: str, body: dict):
    """파이프 내 개별 봇 활성화/비활성화."""
    from src.strategy import StrategyManager
    enabled = body.get("enabled", True)
    mgr = StrategyManager()
    s = mgr.get(pipe_id)
    if not s:
        return {"success": False, "error": "파이프 없음"}
    for bot in s.bots:
        if bot.get("bot_id") == bot_id:
            bot["enabled"] = enabled
            mgr._save()
            return {"success": True, "bot_id": bot_id, "enabled": enabled}
    return {"success": False, "error": f"봇 '{bot_id}' 없음"}


@router.get("/pipes/{pipe_id}/bot-status")
async def get_bot_status(pipe_id: str):
    """파이프에 연결된 모든 봇의 상태 + UI 위젯 스펙 반환."""
    from src.strategy import StrategyManager
    from src.bots import registry as bot_reg
    from src.db import get_db

    mgr_s = StrategyManager()
    s = mgr_s.get(pipe_id)
    if not s:
        return {"success": False, "error": "파이프 없음"}

    # 활성 봇 + 비활성 봇 모두 포함 (비활성은 running=false)
    all_bots_in_pipe = [{
        "bot_id": b["bot_id"],
        "ratio": b.get("ratio", 0),
        "budget": int(s.budget * b.get("ratio", 0)),
        "params": b.get("params", {}),
        "enabled": b.get("enabled", True),
    } for b in s.bots] if s.bots else []
    active = [b for b in all_bots_in_pipe if b.get("enabled", True) is not False]
    if not all_bots_in_pipe:
        return {"success": False, "error": "봇 없음"}

    def _widget_dict(w) -> dict:
        return {
            "id": w.id, "type": w.type, "label": w.label,
            "size": w.size, "data_key": w.data_key,
            "format": w.format, "color_rule": w.color_rule,
            "columns": w.columns,
        }

    def _interval_fields(bot_id: str, params: dict) -> dict:
        spec = bot_reg.interval_spec(bot_id)
        if not spec:
            return {}
        effective = params.get("interval_seconds", spec["default"])
        return {
            "interval_seconds": int(effective),
            "interval_min": spec["min"],
            "interval_max": spec["max"],
        }

    db = get_db()
    results = []
    for ab in active:
        bot_id = ab["bot_id"]
        bot_info = bot_reg.get(bot_id)

        # UI Spec
        ui_spec = None
        if bot_info and bot_info.ui and bot_info.ui.widgets:
            ui_spec = {
                "grid_cols": bot_info.ui.grid_cols,
                "widgets": [_widget_dict(w) for w in bot_info.ui.widgets],
            }

        # DB 상태
        instance_key = f"{pipe_id}:{bot_id}"
        db_state = db.load_bot_state(instance_key) or db.load_bot_state(bot_id) or {}

        # 파이프가 꺼져 있으면 Redis/과거 live 상태를 신뢰하지 않는다.
        if not getattr(s, "active", False):
            status = {
                "running": False,
                "holding": {
                    "code": db_state.get("holding_code", ""),
                    "qty": db_state.get("holding_qty", 0),
                    "price": db_state.get("holding_price", 0),
                },
                "positions": db_state.get("positions", []),
                "pnl": {
                    "realized": db_state.get("realized_pnl", 0),
                    "unrealized": 0,
                    "total": db_state.get("realized_pnl", 0),
                },
                "scan_count": db_state.get("scan_count", 0),
                "last_analysis": db_state.get("last_analysis", {}),
                "last_preflight": db_state.get("last_preflight", {}),
                "position_reviews": db_state.get("position_reviews", []),
                "debate_log": db_state.get("debate_log", []),
                "recent_trades": db_state.get("recent_trades", []),
            }
            status["position_reports"] = _build_position_reports(bot_id, status, db_state)
            results.append({
                "bot_id": bot_id,
                "bot_name": bot_info.name if bot_info else bot_id,
                "ratio": ab["ratio"],
                "budget": ab["budget"],
                "ui": ui_spec,
                "status": status,
            })
            continue

        # BotManager live 상태 (instance_id 우선, bot_id 부분매칭 fallback)
        live_status = None
        try:
            mgr_b = get_bot_manager()
            # 1) 정확 매칭: pipe_id:bot_id
            bot_inst = mgr_b._polling_bots.get(instance_key)
            if bot_inst:
                live_status = bot_inst.status()
            else:
                # 2) 부분 매칭 fallback (레거시)
                for name, bot in mgr_b._polling_bots.items():
                    if bot_id in name:
                        live_status = bot.status()
                        break
        except Exception:
            pass

        # 3) Redis 상태 (독립 프로세스 실행 시)
        if not live_status:
            try:
                from src.services.redis_bus import get_bus
                redis_status = get_bus().get_status("pipe", pipe_id)
                if redis_status and isinstance(redis_status.get("bot_statuses"), dict):
                    live_status = redis_status["bot_statuses"].get(bot_id)
            except Exception:
                pass

        # 4) PID 파일 기반 프로세스 생존 확인 (Redis 만료 시 최종 fallback)
        if not live_status:
            import os as _os
            from src.config import PROJECT_ROOT
            pid_file = PROJECT_ROOT / "data" / "pids" / f"pipe_{pipe_id}.pid"
            process_alive = False
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text().strip())
                    _os.kill(pid, 0)
                    process_alive = True
                except (ProcessLookupError, ValueError, PermissionError):
                    pid_file.unlink(missing_ok=True)

            status = {
                "running": process_alive,
                "holding": {"code": db_state.get("holding_code", ""), "qty": db_state.get("holding_qty", 0), "price": db_state.get("holding_price", 0)},
                "positions": db_state.get("positions", []),
                "pnl": {"realized": db_state.get("realized_pnl", 0), "unrealized": 0, "total": db_state.get("realized_pnl", 0)},
                "scan_count": db_state.get("scan_count", 0),
                "last_analysis": db_state.get("last_analysis", {}),
                "last_preflight": db_state.get("last_preflight", {}),
                "position_reviews": db_state.get("position_reviews", []),
                "debate_log": db_state.get("debate_log", []),
                "recent_trades": db_state.get("recent_trades", []),
            }
        else:
            status = live_status

        status["position_reports"] = _build_position_reports(bot_id, status, db_state)

        results.append({
            "bot_id": bot_id,
            "bot_name": bot_info.name if bot_info else bot_id,
            "ratio": ab["ratio"],
            "budget": ab["budget"],
            "ui": ui_spec,
            "status": status,
            **_interval_fields(bot_id, ab.get("params", {})),
        })

    # 비활성 봇도 포함 (running: false)
    active_ids = {b["bot_id"] for b in active}
    for ab in all_bots_in_pipe:
        if ab["bot_id"] not in active_ids:
            bot_info = bot_reg.get(ab["bot_id"])
            results.append({
                "bot_id": ab["bot_id"],
                "bot_name": bot_info.name if bot_info else ab["bot_id"],
                "ratio": ab["ratio"],
                "budget": ab["budget"],
                "ui": None,
                "status": {"running": False, "enabled": False, "scan_count": 0,
                           "holding": {}, "pnl": {"realized": 0, "unrealized": 0, "total": 0}},
                **_interval_fields(ab["bot_id"], ab.get("params", {})),
            })

    return {"success": True, "pipe_id": pipe_id, "bots": results}


class UpdatePipeRequest(BaseModel):
    budget: int | None = None


@router.put("/pipes/{pipe_id}")
async def update_pipe(pipe_id: str, req: UpdatePipeRequest):
    """파이프 예산 변경."""
    from src.strategy import StrategyManager
    mgr = StrategyManager()
    s = mgr.get(pipe_id)
    if not s:
        return {"success": False, "error": "파이프 없음"}
    updates = {}
    if req.budget is not None:
        updates["budget"] = req.budget
        updates["cash"] = req.budget  # 예산 변경 시 현금도 리셋
    mgr.update_strategy(pipe_id, **updates)
    return {"success": True, "updated": updates}


@router.delete("/pipes/{pipe_id}")
async def delete_pipe(pipe_id: str):
    from src.strategy import StrategyManager
    mgr = StrategyManager()
    mgr.delete_strategy(pipe_id)
    return {"success": True}


# ── 조건식 ──

@router.get("/conditions")
async def get_conditions():
    """모든 활성 전략의 조건식."""
    mgr = get_bot_manager()
    all_conditions = []
    for sid, runner in mgr._runners.items():
        for c in runner.evaluator.conditions:
            all_conditions.append({
                "id": c.id, "stock_code": c.stock_code, "stock_name": c.stock_name,
                "action": c.action, "strategy_id": c.strategy_id,
                "rules": [{"indicator": r.indicator, "operator": r.operator, "value": r.value}
                          for r in c.rules],
                "logic": c.logic, "amount": c.amount, "reason": c.reason,
            })
    return {"conditions": all_conditions}


@router.get("/conditions/files")
async def list_condition_files():
    settings.ensure_dirs()
    files = sorted(settings.conditions_dir.glob("*.json"), reverse=True)
    return {"files": [f.name for f in files]}


# ── AI 분석 ──

@router.get("/models")
async def get_models():
    """프로바이더별 사용 가능한 모델 목록."""
    from src.ai.analyzer import PROVIDER_MODELS
    return {"models": PROVIDER_MODELS}


@router.post("/analyze")
async def analyze(req: AnalyzeRequest):
    await broker.publish("system", {"message": "AI 분석 시작..."})

    provider = AIProvider(req.provider)
    analyzer = AIAnalyzer(provider=provider, model=req.model, timeout=180)

    prompt = build_analysis_prompt(
        market_data={},
        user_rules=req.rules,
        target_stocks=req.stocks if req.stocks else None,
    )

    # AI 호출은 블로킹이므로 스레드에서 실행
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(None, analyzer.analyze, prompt)

    if not response:
        await broker.publish("error", {"message": "AI 응답 없음"})
        return {"error": "AI 응답 없음"}

    data = parse_ai_response(response)
    if not data:
        await broker.publish("error", {"message": "AI 응답 파싱 실패"})
        return {"error": "AI 응답 파싱 실패", "raw": response[:500]}

    path = save_conditions(data)
    n = len(data.get("conditions", []))
    await broker.publish("system", {"message": f"조건식 {n}개 생성: {path.name}"})

    return {
        "file": path.name,
        "analysis": data.get("analysis", ""),
        "conditions": data.get("conditions", []),
    }


# ── 세션 관리 ──

from src.session import SessionManager

_session_mgr = SessionManager()


@router.get("/sessions")
async def list_sessions(provider: str = ""):
    return {"sessions": _session_mgr.list_sessions(provider=provider)}


@router.post("/sessions")
async def create_session(provider: str = "claude"):
    session = _session_mgr.create(provider=provider)
    return session


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    session = _session_mgr.get(session_id)
    if not session:
        return {"error": "세션 없음"}
    return session


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    _session_mgr.delete(session_id)
    return {"ok": True}


# ── AI 채팅 ──

@router.post("/chat")
async def chat(req: ChatRequest):
    """자연어 메시지를 투자 에이전트에게 전달 — Claude Code 내장 세션으로 맥락 보존."""
    await broker.publish("system", {"message": f"에이전트 호출: {req.message[:50]}..."})

    # 세션 관리: 없으면 자동 생성
    session_id = req.session_id
    if not session_id:
        session = _session_mgr.create(provider=req.provider)
        session_id = session["id"]

    # 사용자 메시지 기록 (웹 UI 표시용)
    _session_mgr.add_message(session_id, "user", req.message)

    # Claude Code 세션 ID 가져오기 (resume용)
    session_data = _session_mgr.get(session_id)
    claude_sid = session_data.get("claude_session_id", "") if session_data else ""

    provider = AIProvider(req.provider)
    analyzer = AIAnalyzer(provider=provider, model=req.model)  # 기본 600초 타임아웃

    # 에이전트 호출 — Claude Code가 대화 이력을 내부적으로 관리
    logger.info("채팅 → Claude 에이전트: session=%s msg=%s...", session_id, req.message[:50])
    loop = asyncio.get_event_loop()
    response, used_claude_sid = await loop.run_in_executor(
        None, analyzer.analyze, req.message, claude_sid
    )

    # Claude Code 세션 ID 저장 (다음 호출에서 resume)
    if used_claude_sid and used_claude_sid != claude_sid:
        _session_mgr.set_claude_session_id(session_id, used_claude_sid)

    if not response:
        logger.error("AI 응답 없음: session=%s, claude_sid=%s", session_id, used_claude_sid)
        return {"error": "AI 응답 없음", "reply": "에이전트로부터 응답을 받지 못했습니다. 서버 로그를 확인하세요.",
                "session_id": session_id}

    # AI 응답 기록 (웹 UI 표시용)
    _session_mgr.add_message(session_id, "assistant", response, provider=req.provider)

    # 응답에서 JSON 조건식 추출 시도
    data = parse_ai_response(response)
    if data and data.get("conditions"):
        path = save_conditions(data)
        n = len(data["conditions"])

        # 전략 자동 등록 + 봇 활성화
        strategy_block = data.get("strategy", {})
        activated_strategy = None
        if strategy_block.get("name"):
            from src.strategy import StrategyManager
            mgr = StrategyManager()
            bot = get_bot_manager()
            try:
                s = mgr.create_strategy(
                    name=strategy_block["name"],
                    budget=strategy_block.get("budget", 0),
                    description=data.get("analysis", ""),
                )
                mgr.link_conditions(s.id, path.name)
                ok, msg = bot.activate(s.id)
                if ok and not bot._running:
                    await bot.start()
                activated_strategy = s.id
                await broker.publish("system", {
                    "message": f"전략 '{s.name}' 생성 + 봇 활성화 ({n}개 조건식)"
                })
            except ValueError as e:
                await broker.publish("system", {"message": f"전략 생성 실패: {e}"})
        else:
            await broker.publish("system", {"message": f"조건식 {n}개 생성: {path.name}"})

        return {
            "reply": data.get("analysis", "조건식을 생성했습니다."),
            "file": path.name,
            "conditions": data["conditions"],
            "session_id": session_id,
            "strategy_id": activated_strategy,
        }

    return {"reply": response, "conditions": [], "session_id": session_id}


@router.get("/chat/stream")
async def chat_stream(
    request: Request,
    message: str,
    provider: str = "claude",
    model: str = "",
    session_id: str = "",
):
    """SSE 스트리밍 채팅 — Claude 전용."""
    if provider != "claude":
        return {"error": "스트리밍은 Claude만 지원합니다"}

    await broker.publish("system", {"message": f"에이전트 호출: {message[:50]}..."})

    if not session_id:
        session = _session_mgr.create(provider="claude")
        session_id = session["id"]

    _session_mgr.add_message(session_id, "user", message)

    session_data = _session_mgr.get(session_id)
    claude_sid = session_data.get("claude_session_id", "") if session_data else ""

    generate, _ = await stream_claude_chat(
        prompt=message, session_id=claude_sid, model=model)
    # used_sid는 done chunk가 발생하기 전에 반환되어 의미 없음 — 폐기.
    # 진짜 agentcli session_id는 아래 wrapped()에서 done payload로부터 추출.

    async def wrapped():
        full_text = ""
        new_claude_sid = ""
        async for chunk in generate():
            yield chunk
            if "event: done" in chunk:
                try:
                    data_line = chunk.split("data: ", 1)[1].split("\n")[0]
                    done = json.loads(data_line)
                    full_text = done.get("text", "")
                    new_claude_sid = done.get("session_id", "") or ""
                except (IndexError, json.JSONDecodeError):
                    pass
        if full_text:
            _session_mgr.add_message(session_id, "assistant", full_text, provider="claude")
        # agentcli가 발급한 conversation UUID를 외부 session_NNN에 페어링.
        # 다음 요청에서 이 UUID가 다시 agentcli로 전달돼 대화가 이어진다.
        if new_claude_sid and new_claude_sid != claude_sid:
            _session_mgr.set_claude_session_id(session_id, new_claude_sid)
        yield f"event: session\ndata: {json.dumps({'session_id': session_id}, ensure_ascii=False)}\n\n"

    return StreamingResponse(wrapped(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── 투자 모드 제어 ──

@router.get("/mode")
async def get_mode():
    return tm.mode_info()


@router.post("/mode/set-disabled")
async def set_disabled():
    tm.set_disabled()
    await broker.publish("system", {"message": "투자 모드 → DISABLED (주문 차단)"})
    return {"mode": "disabled"}


@router.post("/mode/set-paper")
async def set_paper():
    tm.set_paper()
    await broker.publish("system", {"message": "투자 모드 → PAPER (모의투자)"})
    return {"mode": "paper"}


@router.post("/mode/request-live")
async def request_live():
    code = tm.request_live()
    await broker.publish("system", {"message": "LIVE 모드 전환 요청됨 — 확인 코드를 입력하세요"})
    return {"unlock_code": code, "message": f"확인 코드: {code} — 이 코드를 입력하여 전환을 확인하세요"}


class ConfirmLiveRequest(BaseModel):
    code: str


@router.post("/mode/confirm-live")
async def confirm_live(req: ConfirmLiveRequest):
    ok, msg = tm.confirm_live(req.code)
    if ok:
        await broker.publish("system", {"message": "투자 모드 → LIVE (실전투자) ⚠️"})
    return {"success": ok, "message": msg, "mode": tm.current_mode()}


class MaxOrderRequest(BaseModel):
    amount: int


@router.post("/mode/max-order")
async def set_max_order(req: MaxOrderRequest):
    tm.set_max_order_amount(req.amount)
    return {"max_order_amount": req.amount}


class DerivativeEtfRequest(BaseModel):
    enabled: bool = False


@router.post("/mode/derivative-etf")
async def set_derivative_etf_mode(req: DerivativeEtfRequest):
    """파생ETF(인버스/레버리지) 거래 허용 토글 — 상급 옵션, 기본 OFF."""
    info = tm.set_derivative_etf(req.enabled)
    await broker.publish("system", {
        "message": f"파생ETF 거래(인버스·레버리지) → {'허용' if req.enabled else '차단'} (상급 옵션)"
    })
    return {"ok": True, "allow_derivative_etf": info.get("allow_derivative_etf", False)}


# ── 계좌 현황 ──

@router.get("/account/balance")
async def get_account_balance():
    """주식 잔고 — KIS 플래핑 대응 (의심-빈-응답 재시도 + 최종 정상값 캐시).

    기존 {holdings, summary} 형태 호환 + stale/as_of/balance_error 추가.
    stale=true면 holdings/summary는 마지막 정상값 — Account 카드와
    수동 주문 보유 드롭다운이 그대로 사용 가능. 캐시도 없는 완전 실패는
    빈 holdings/summary (UI는 기존처럼 '-' 표시).
    """
    res = _get_balance_resilient()
    return {"holdings": res["holdings"], "summary": res["summary"],
            "stale": res["stale"], "as_of": res["as_of"],
            "balance_error": res["balance_error"]}


@router.get("/account/executions")
async def get_account_executions(start: str = "", end: str = ""):
    """체결 내역 조회. start/end는 YYYYMMDD. 비면 당일."""
    from src.kis.market import KISMarket
    m = KISMarket()
    try:
        return {"executions": m.get_executions(start_date=start, end_date=end)}
    finally:
        m.close()


# ── 수동 주문 ──

class ManualOrderRequest(BaseModel):
    side: str            # "buy" | "sell"
    code: str            # 6자리 종목코드
    qty: int
    price: int = 0       # 0 = 시장가


@router.post("/manual/order")
async def manual_order(req: ManualOrderRequest):
    """대시보드 수동 매수/매도 — 봇 ledger 오염 방지를 위해 source='manual'로 기록."""
    side = (req.side or "").strip().lower()
    code = (req.code or "").strip()
    if side not in ("buy", "sell"):
        return {"success": False, "error": f"side는 buy/sell만 가능합니다: {req.side}"}
    if not (code.isascii() and code.isdigit() and len(code) == 6):
        return {"success": False, "error": f"종목코드는 6자리 숫자여야 합니다: {req.code}"}
    if req.qty <= 0:
        return {"success": False, "error": "수량은 1 이상이어야 합니다"}
    if req.price < 0:
        return {"success": False, "error": "가격은 0 이상이어야 합니다 (0=시장가)"}

    # 주문액 추정 — 시장가는 현재가 조회 (체결가 기록 폴백에도 사용)
    cur_price = 0
    if req.price > 0:
        est_amount = req.price * req.qty
    else:
        from src.kis.market import KISMarket
        m = KISMarket()
        try:
            info = m.get_price(code)
            cur_price = info.get("price", 0) if info else 0
        finally:
            m.close()
        # 현재가 없이는 max_order_amount 캡이 공허 통과(est_amount=0)되므로 즉시 거부
        if cur_price <= 0:
            return {"success": False, "error": "현재가 조회 실패 — 시장가 주문 불가"}
        est_amount = cur_price * req.qty

    # 공통 주문 게이트 — DISABLED 차단 + 최대 주문액 검증
    ok, msg = tm.validate_order(code, est_amount, strategy_id="manual")
    if not ok:
        return {"success": False, "error": msg}

    from src.kis.order import KISOrder, OrderType
    order_type = OrderType.MARKET if req.price == 0 else OrderType.LIMIT
    order = KISOrder()
    try:
        if side == "buy":
            result = order.buy(stock_code=code, quantity=req.qty,
                               price=req.price, order_type=order_type)
        else:
            result = order.sell(stock_code=code, quantity=req.qty,
                                price=req.price, order_type=order_type)
    finally:
        order.close()

    # 기록 가격: LIMIT=요청가, MARKET=체결가(OrderResult.price, 없으면 현재가 폴백)
    log_price = req.price if req.price > 0 else (result.price or cur_price)
    try:
        from src.db import get_db
        from src.kis.master import stock_name_by_code
        get_db().log_trade("manual", code, stock_name_by_code(code), side,
                           req.qty, log_price,
                           order_no=result.order_no, success=result.success,
                           reason="수동 주문 (대시보드)", source="manual")
    except Exception:
        logger.exception("수동 주문 DB 기록 실패: %s %s", side, code)

    if not result.success:
        return {"success": False, "error": result.message or "주문 실패"}

    label = "매수" if side == "buy" else "매도"
    await broker.publish("system", {
        "message": f"[수동 주문] {code} {req.qty}주 {label} 성공 (주문번호: {result.order_no})"})
    return {"success": True, "order_no": result.order_no, "side": side,
            "code": code, "qty": req.qty, "price": log_price}


@router.get("/account/pending")
async def get_account_pending():
    from src.kis.market import KISMarket
    m = KISMarket()
    try:
        return {"pending": m.get_pending_orders()}
    finally:
        m.close()


# ── API/MCP 로그 ──

from src.api_log import get_mcp_logs, get_api_logs, get_all_logs


@router.get("/logs/mcp")
async def mcp_logs(count: int = 50):
    return {"logs": get_mcp_logs(count)}


@router.get("/logs/api")
async def api_logs(count: int = 50):
    return {"logs": get_api_logs(count)}


@router.get("/logs/all")
async def all_logs(count: int = 50):
    return {"logs": get_all_logs(count)}


@router.get("/api-stats")
async def api_stats():
    """API 레이트 리미터 + 캐시 상태."""
    from src.kis.rate_limiter import get_limiter
    from src.kis.market_cache import get_cache
    return {
        "rate_limiter": get_limiter().stats,
        "cache": get_cache().stats,
    }


# ── 전략 관리 ──

from src.strategy import StrategyManager
from src.decision_log import DecisionLog


@router.get("/strategies")
async def list_strategies_api():
    mgr = StrategyManager()
    strategies = mgr.list_strategies()
    return {
        "total_budget": mgr.portfolio.total_budget,
        "allocated": mgr.portfolio.allocated(),
        "unallocated": mgr.portfolio.unallocated(),
        "strategies": [
            {
                "id": s.id, "name": s.name, "description": s.description,
                "budget": s.budget, "used": s.used, "remaining": s.remaining(),
                "active": s.active, "conditions_file": s.conditions_file,
            }
            for s in strategies
        ],
    }


@router.get("/strategies/{strategy_id}/perf")
async def strategy_performance(strategy_id: str):
    mgr = StrategyManager()
    s = mgr.get(strategy_id)
    if not s:
        return {"error": f"전략 {strategy_id} 없음"}
    tl = TradeLogger()
    summary = tl.summary(strategy_id)
    trades = tl.by_strategy(strategy_id)
    return {
        "strategy": {"id": s.id, "name": s.name, "budget": s.budget, "used": s.used},
        "performance": summary,
        "recent_trades": trades[-10:],
    }


@router.get("/portfolio")
async def portfolio_status():
    mgr = StrategyManager()
    p = mgr.portfolio
    return {
        "total_budget": p.total_budget,
        "allocated": p.allocated(),
        "unallocated": p.unallocated(),
        "strategies": [
            {"id": s.id, "name": s.name, "budget": s.budget, "used": s.used,
             "usage_pct": (s.used / s.budget * 100) if s.budget > 0 else 0}
            for s in p.strategies.values()
        ],
    }


# ── 보유종목 상세 (현재가 + 손익 포함) ──

def split_holdings_by_bot(rows: list, code: str, total_qty: int, aggregate_avg: int) -> list[dict]:
    """Reconstruct per-bot ownership of a single stock from trade history.

    Cross-bot FIFO: when a bot sells more than it bought, the excess falls
    through to other bots' lots in insertion order. Synthetic rows with
    reason='phantom-cleanup' are skipped — they're per-bot ledger corrections,
    not broker transactions, so applying them here would double-deduct.
    """
    lots: dict[str, dict] = {}

    def _ensure(bot_id: str) -> dict:
        bot_id = bot_id or "unknown"
        if bot_id not in lots:
            lots[bot_id] = {"bot_id": bot_id, "qty": 0, "cost": 0}
        return lots[bot_id]

    for row in rows:
        # Support both sqlite Row and plain dicts (for tests)
        get = row.__getitem__ if hasattr(row, "keys") else (lambda k, _r=row: _r.get(k))
        reason = ""
        try:
            reason = get("reason") or ""
        except (KeyError, IndexError):
            reason = ""
        if reason == "phantom-cleanup":
            continue
        side = get("side")
        qty = int(get("quantity") or 0)
        try:
            price = int(get("price") or 0)
        except (TypeError, ValueError):
            price = 0
        try:
            bot_id = (get("source") or "unknown")
        except (KeyError, IndexError):
            bot_id = "unknown"
        if qty <= 0:
            continue
        if side == "buy":
            lot = _ensure(bot_id)
            lot["qty"] += qty
            lot["cost"] += qty * price
            continue

        if side == "sell":
            remaining = qty
            # 같은 봇 lot 우선, 부족하면 다른 봇 lot에서 오래된 순으로 차감.
            order = [bot_id] + [b for b in lots.keys() if b != bot_id]
            for bid in order:
                lot = lots.get(bid)
                if not lot or lot["qty"] <= 0 or remaining <= 0:
                    continue
                take = min(remaining, lot["qty"])
                avg = lot["cost"] / lot["qty"] if lot["qty"] > 0 else 0
                lot["qty"] -= take
                lot["cost"] -= int(round(avg * take))
                remaining -= take

    result = [
        {"bot_id": lot["bot_id"], "qty": int(lot["qty"]),
         "avg_price": int(round(lot["cost"] / lot["qty"])) if lot["qty"] > 0 else 0}
        for lot in lots.values()
        if int(lot["qty"]) > 0
    ]

    attributed_qty = sum(x["qty"] for x in result)
    if attributed_qty < total_qty:
        result.append({
            "bot_id": "unknown",
            "qty": total_qty - attributed_qty,
            "avg_price": aggregate_avg,
        })
    elif attributed_qty > total_qty and attributed_qty > 0:
        scale = total_qty / attributed_qty
        adjusted = []
        remaining = total_qty
        for item in result:
            q = min(remaining, max(0, int(round(item["qty"] * scale))))
            if q > 0:
                adjusted.append({**item, "qty": q})
                remaining -= q
        if remaining > 0 and adjusted:
            adjusted[0]["qty"] += remaining
        result = adjusted

    return result or [{"bot_id": "", "qty": total_qty, "avg_price": aggregate_avg}]


def split_holdings_by_bot_positions(
    bot_statuses: list[dict],
    code: str,
    total_qty: int,
    aggregate_avg: int,
) -> list[dict]:
    """Fallback ownership from each bot's current positions.

    Trade replay is still the primary source. This covers cases where broker
    holdings exist but the trade rows cannot attribute the remaining shares.
    """
    if total_qty <= 0:
        return []

    lots: dict[str, dict] = {}

    def _ensure(bot_id: str) -> dict:
        bot_id = bot_id or "unknown"
        if bot_id not in lots:
            lots[bot_id] = {"bot_id": bot_id, "qty": 0, "cost": 0}
        return lots[bot_id]

    for row in bot_statuses:
        status = row.get("status") if isinstance(row.get("status"), dict) else row
        bot_id = row.get("bot_id") or status.get("bot_id") or "unknown"
        raw_positions = status.get("positions") or []
        if not raw_positions and isinstance(status.get("extra"), dict):
            raw_positions = status["extra"].get("positions") or []
        for pos in raw_positions:
            pos_code = pos.get("code") or pos.get("symbol") or ""
            if pos_code != code:
                continue
            qty = int(_to_float(pos.get("qty", pos.get("quantity", 0)), 0))
            if qty <= 0:
                continue
            entry = _to_float(
                pos.get("entry_price", pos.get("price", pos.get("avg_price", aggregate_avg))),
                aggregate_avg,
            )
            lot = _ensure(bot_id)
            lot["qty"] += qty
            lot["cost"] += qty * int(entry)

    result = [
        {
            "bot_id": lot["bot_id"],
            "qty": int(lot["qty"]),
            "avg_price": int(round(lot["cost"] / lot["qty"])) if lot["qty"] > 0 else aggregate_avg,
        }
        for lot in lots.values()
        if int(lot["qty"]) > 0
    ]
    if not result:
        return []

    attributed_qty = sum(x["qty"] for x in result)
    if attributed_qty < total_qty:
        result.append({
            "bot_id": "unknown",
            "qty": total_qty - attributed_qty,
            "avg_price": aggregate_avg,
        })
        return result

    if attributed_qty <= total_qty:
        return result

    scale = total_qty / attributed_qty
    adjusted = []
    remaining = total_qty
    for item in result:
        q = min(remaining, max(0, int(round(item["qty"] * scale))))
        if q > 0:
            adjusted.append({**item, "qty": q})
            remaining -= q
    if remaining > 0 and adjusted:
        adjusted[0]["qty"] += remaining
    return adjusted


def _pipe_bot_position_statuses(pipe_id: str, strategy, db) -> list[dict]:
    """Collect current per-bot positions from live, Redis, or saved state."""
    try:
        bot_entries = strategy.active_bots()
    except Exception:
        bot_entries = strategy.bots or []

    redis_statuses = {}
    try:
        from src.services.redis_bus import get_bus
        redis_status = get_bus().get_status("pipe", pipe_id) or {}
        if isinstance(redis_status.get("bot_statuses"), dict):
            redis_statuses = redis_status["bot_statuses"]
    except Exception:
        redis_statuses = {}

    manager_bots = {}
    try:
        manager_bots = get_bot_manager()._polling_bots
    except Exception:
        manager_bots = {}

    statuses = []
    for entry in bot_entries:
        bot_id = entry.get("bot_id", "")
        if not bot_id:
            continue
        instance_key = f"{pipe_id}:{bot_id}"
        db_state = db.load_bot_state(instance_key) or db.load_bot_state(bot_id) or {}

        live_status = None
        bot_inst = manager_bots.get(instance_key)
        if bot_inst:
            try:
                live_status = bot_inst.status()
            except Exception:
                live_status = None
        if not live_status:
            live_status = redis_statuses.get(bot_id)
        status = live_status or db_state or {}
        positions = _normalize_positions(status, db_state)
        statuses.append({"bot_id": bot_id, "positions": positions})
    return statuses


@router.get("/pipes/{pipe_id}/holdings")
async def get_pipe_holdings(pipe_id: str):
    """파이프 보유종목 + 현재가 + 손익 조회."""
    from src.strategy import StrategyManager
    from src.kis.market import KISMarket

    mgr = StrategyManager()
    s = mgr.get(pipe_id)
    if not s:
        return {"holdings": []}

    market = KISMarket()
    result = []
    total_eval = 0
    total_cost = 0

    from src.db import get_db
    db = get_db()
    bot_position_statuses = _pipe_bot_position_statuses(pipe_id, s, db)

    def split_by_bot(code: str, total_qty: int, aggregate_avg: int) -> list[dict]:
        try:
            rows = db._conn.execute(
                """SELECT side, quantity, price, source, reason
                   FROM trades
                   WHERE pipe_id=? AND stock_code=?
                   ORDER BY timestamp ASC""",
                (pipe_id, code),
            ).fetchall()
        except Exception:
            rows = []
        splits = split_holdings_by_bot(rows, code, total_qty, aggregate_avg)
        only_unknown = all((x.get("bot_id") or "unknown") in ("", "unknown") for x in splits)
        if only_unknown:
            position_splits = split_holdings_by_bot_positions(
                bot_position_statuses, code, total_qty, aggregate_avg
            )
            if position_splits:
                return position_splits
        return splits

    for h in (s.holdings or []):
        code = h.get("code", "")
        qty = h.get("qty", 0)
        avg_price = h.get("avg_price", 0)
        cost = avg_price * qty

        current_price = avg_price
        stock_name = h.get("name", "")
        try:
            info = market.get_price(code)
            if info and info.get("price"):
                current_price = info["price"]
            if info and info.get("name"):
                stock_name = stock_name or info["name"]
        except Exception:
            pass
        if not stock_name:
            try:
                from src.strategy import stock_name_for
                stock_name = stock_name_for(code)
            except Exception:
                stock_name = ""

        for split in split_by_bot(code, qty, avg_price):
            row_qty = split["qty"]
            row_avg = split["avg_price"] or avg_price
            row_cost = row_avg * row_qty
            eval_amount = current_price * row_qty
            pnl = eval_amount - row_cost
            pnl_pct = ((current_price - row_avg) / row_avg * 100) if row_avg > 0 else 0

            result.append({
                "code": code,
                "name": stock_name,
                "qty": row_qty,
                "avg_price": row_avg,
                "current_price": current_price,
                "eval_amount": eval_amount,
                "cost": row_cost,
                "pnl": pnl,
                "pnl_pct": round(pnl_pct, 2),
                "bought_at": h.get("bought_at", ""),
                "bot_id": split["bot_id"],
            })
        total_eval += current_price * qty
        total_cost += cost

    market.close()
    return {
        "holdings": result,
        "total_eval": total_eval,
        "total_cost": total_cost,
        "total_pnl": total_eval - total_cost,
        "total_pnl_pct": round((total_eval - total_cost) / total_cost * 100, 2) if total_cost > 0 else 0,
        "cash": s.cash,
        "total_asset": s.cash + total_eval,
    }


# ── 토큰 사용량 ──

@router.get("/token-usage")
async def token_usage(bot_id: str = "", days: int = 7):
    """AI 봇 토큰 사용량 조회."""
    from src.bots.ai_caller import get_token_stats
    return get_token_stats(bot_id, days)


@router.get("/bot-roi")
async def bot_roi(days: int = 14):
    """봇별 ROI 분석 (토큰 비용 vs 실현손익).

    Returns:
        {
          "bots": [
            {bot_id, trades, wins, losses, win_rate, realized_pnl_krw,
             tokens, calls, est_cost_usd, realized_usd, roi_usd, status}
          ],
          "summary": {total_pnl, total_cost, net_profit, period_days}
        }
    """
    from src.bots.ai_caller import get_token_stats
    from src.db import get_db
    from datetime import datetime, timedelta

    db = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    token_stats = get_token_stats(days=days)

    # 모든 거래 봇 조회
    rows = db._conn.execute("""
        SELECT
            source as bot_id,
            COUNT(*) as trades,
            SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) as buys,
            SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END) as sells,
            SUM(CASE WHEN side='sell' AND realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN side='sell' AND realized_pnl < 0 THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN side='sell' THEN realized_pnl ELSE 0 END) as realized_pnl_krw
        FROM trades
        WHERE source != '' AND timestamp >= ?
        GROUP BY source
        ORDER BY realized_pnl_krw DESC
    """, (cutoff,)).fetchall()

    # 환율/가격 가정
    USD_PER_KRW = 1 / 1400
    # Claude opus 4.6 평균 비용 (input 60%, output 40%)
    COST_PER_TOKEN = (0.6 * 15 + 0.4 * 75) / 1_000_000

    bots = []
    total_pnl = 0
    total_cost = 0
    for r in rows:
        d = dict(r)
        bid = d["bot_id"]
        wins = d["wins"] or 0
        losses = d["losses"] or 0
        total = wins + losses
        d["win_rate"] = wins / total if total > 0 else 0
        d["realized_pnl_krw"] = d["realized_pnl_krw"] or 0

        # 토큰 비용 (AI 봇만)
        tinfo = token_stats.get("by_bot", {}).get(bid, {})
        tokens = tinfo.get("tokens", 0)
        calls = tinfo.get("calls", 0)
        est_cost_usd = round(tokens * COST_PER_TOKEN, 4)
        realized_usd = round(d["realized_pnl_krw"] * USD_PER_KRW, 4)
        roi_usd = round(realized_usd - est_cost_usd, 4)

        if est_cost_usd == 0:
            status = "non_ai"  # 토큰 비용 없음 (전통 봇)
        elif roi_usd > 0:
            status = "profitable"
        elif roi_usd < -1:
            status = "loss"
        else:
            status = "marginal"

        d.update({
            "tokens": tokens,
            "calls": calls,
            "est_cost_usd": est_cost_usd,
            "realized_usd": realized_usd,
            "roi_usd": roi_usd,
            "status": status,
        })
        bots.append(d)
        total_pnl += d["realized_pnl_krw"]
        total_cost += est_cost_usd

    return {
        "bots": bots,
        "summary": {
            "total_pnl_krw": total_pnl,
            "total_pnl_usd": round(total_pnl * USD_PER_KRW, 2),
            "total_cost_usd": round(total_cost, 2),
            "net_profit_usd": round(total_pnl * USD_PER_KRW - total_cost, 2),
            "period_days": days,
        }
    }


BENCHMARK_CODE = "069500"  # KODEX 200 — KOSPI 200 추종, 한국 표준 벤치마크
BENCHMARK_NAME = "KODEX 200"


def _fetch_kis_balance_for_pipe(pipe_id: str) -> dict:
    """KIS 잔고 조회 — 테스트에서 monkeypatch 가능하도록 분리."""
    from src.kis.market import KISMarket
    m = KISMarket()
    try:
        return m.get_balance() or {}
    finally:
        try:
            m.close()
        except Exception:
            pass


def _compute_orphans(pipe_id: str) -> list[dict]:
    """KIS 실잔고 vs 모든 봇 ledger 합산 → 봇이 청구하지 않은 잉여 주식."""
    from src.db import get_db
    from src.bots.ledger import BotLedger

    bal = _fetch_kis_balance_for_pipe(pipe_id) or {}
    kis_holdings = {h.get("stock_code", ""): {
        "qty": int(h.get("quantity", 0) or 0),
        "name": h.get("name", "") or h.get("stock_name", ""),
        "current_price": int(h.get("current_price", 0) or 0),
    } for h in (bal.get("holdings") or []) if h.get("stock_code")}

    db = get_db()
    trades = db.get_trades(pipe_id=pipe_id, limit=20000)
    by_source: dict[str, list[dict]] = {}
    for t in trades:
        src = t.get("source") or ""
        if not src:
            continue
        by_source.setdefault(src, []).append(t)
    bot_qty_by_code: dict[str, int] = {}
    for src, src_trades in by_source.items():
        ledger = BotLedger.from_trades(budget=0, trades=src_trades)
        for p in ledger.positions:
            bot_qty_by_code[p["code"]] = bot_qty_by_code.get(p["code"], 0) + int(p["qty"])

    orphans = []
    for code, info in kis_holdings.items():
        kis_qty = info["qty"]
        bot_qty = bot_qty_by_code.get(code, 0)
        if kis_qty > bot_qty:
            orphans.append({
                "code": code,
                "name": info["name"],
                "kis_qty": kis_qty,
                "bot_attributed_qty": bot_qty,
                "orphan_qty": kis_qty - bot_qty,
                "current_price": info["current_price"],
            })
    return orphans


@router.get("/orphan-holdings")
async def orphan_holdings(pipe_id: str = "strat-001"):
    """KIS 잔고에 있지만 봇 ledger가 청구하지 않는 보유 주식 목록."""
    return {"pipe_id": pipe_id, "orphans": _compute_orphans(pipe_id)}


class OrphanAssignRequest(BaseModel):
    pipe_id: str
    code: str
    bot_id: str
    qty: int
    price: int = 0


@router.post("/orphan-holdings/assign")
async def orphan_assign(req: OrphanAssignRequest):
    """주인 없는 보유 주식을 특정 봇에 귀속.

    `reason='orphan-assign'` BUY 행을 trades에 기입. BotLedger가 이 행을
    인식해 lot만 추가하고 cash는 차감하지 않는다.
    """
    from src.db import get_db
    if req.qty <= 0:
        return {"success": False, "error": "qty must be > 0"}
    if not req.code or not req.code.isdigit() or len(req.code) != 6:
        return {"success": False, "error": "invalid stock code"}
    # 현재가 자동 보강
    price = int(req.price or 0)
    if price <= 0:
        try:
            from src.kis.market import KISMarket
            m = KISMarket()
            try:
                info = m.get_price(req.code) or {}
                price = int(info.get("price", 0) or 0)
            finally:
                m.close()
        except Exception:
            price = 0
    if price <= 0:
        return {"success": False, "error": "price required and could not be fetched"}

    db = get_db()
    trade_id = db.log_trade(
        pipe_id=req.pipe_id,
        stock_code=req.code,
        stock_name="",
        side="buy",
        quantity=req.qty,
        price=price,
        source=req.bot_id,
        reason="orphan-assign",
        success=True,
    )
    return {"success": True, "trade_id": trade_id, "bot_id": req.bot_id,
            "code": req.code, "qty": req.qty, "price": price}


# ── 보유 종합 상황판 ──

# 마지막 정상 KIS 잔고 캐시 — 플래핑(500을 삼킨 빈 holdings 응답) 시 stale 서빙.
# {"holdings": [...], "cash": int, "as_of": iso str}
_LAST_GOOD_BALANCE: dict | None = None


def _fetch_balance_once() -> dict:
    """KIS 잔고 1회 조회 — 테스트에서 KISMarket 클래스 patch 가능."""
    from src.kis.market import KISMarket
    m = KISMarket()
    try:
        return m.get_balance()
    finally:
        try:
            m.close()
        except Exception:
            pass


def _is_suspicious_balance(bal, ledger_total_qty: int) -> tuple[bool, str]:
    """KIS 응답을 truth로 써도 되는지 판정 → (의심 여부, 사유).

    실측 (2026-06-12): KIS 잔고 API 플래핑 중 get_balance()가 500을 삼키고
    빈 holdings를 반환 → 빈 응답을 믿으면 전 종목에 거짓 불일치 경고.
    장부 합산 보유가 1주 이상인데 KIS holdings가 비었거나 전부 qty 0이면 의심.
    """
    if not isinstance(bal, dict) or "holdings" not in bal:
        return True, "KIS 잔고 응답에 holdings가 없습니다"
    if ledger_total_qty <= 0:
        return False, ""
    rows = bal.get("holdings") or []
    if all(int(h.get("quantity", 0) or 0) <= 0 for h in rows):
        return True, "KIS 잔고가 비어 있으나 봇 장부에 보유가 있습니다 (장애 의심)"
    return False, ""


def _fetch_trusted_balance(ledger_total_qty: int) -> tuple[dict | None, str]:
    """신뢰 가능한 KIS 잔고를 최대 2회(원호출 + 재시도 1회) 시도.

    플래핑 실측상 연속 호출 시 다음 호출은 정상인 경우가 많다.
    Returns: (정상 응답 또는 None, 마지막 오류/의심 사유)
    """
    error = ""
    for attempt in (1, 2):
        try:
            candidate = _fetch_balance_once()
        except Exception as e:
            error = str(e) or type(e).__name__
            logger.warning("보유 상황판 KIS 잔고 조회 실패 (시도 %d/2): %s",
                           attempt, e)
            continue
        suspicious, why = _is_suspicious_balance(candidate, ledger_total_qty)
        if not suspicious:
            return candidate, ""
        error = why
        logger.warning("보유 상황판 KIS 잔고 의심 응답 (시도 %d/2): %s",
                       attempt, why)
    return None, error


def _get_balance_resilient(ledger_total_qty: int | None = None) -> dict:
    """재시도 + 최종 정상값 캐시를 적용한 KIS 잔고 조회 (공용).

    /holdings/overview와 /account/balance가 캐시(_LAST_GOOD_BALANCE)를 공유.
    Returns: {"holdings", "summary", "as_of", "stale", "balance_error"}
      - 정상: stale=False, balance_error=None — 캐시 갱신
      - 캐시 서빙: stale=True + 안내문 — holdings/summary는 마지막 정상값
      - 완전 실패(캐시 없음): stale=False + 사유, holdings []/summary {}
    """
    global _LAST_GOOD_BALANCE
    from datetime import datetime

    if ledger_total_qty is None:
        owners = _ledger_owners_by_code()
        ledger_total_qty = sum(o["qty"] for lst in owners.values() for o in lst)

    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    bal, fetch_error = _fetch_trusted_balance(ledger_total_qty)
    if bal is not None:
        result = {"holdings": bal.get("holdings") or [],
                  "summary": bal.get("summary") or {},
                  "as_of": now_iso, "stale": False, "balance_error": None}
        _LAST_GOOD_BALANCE = dict(result)
        return result
    if _LAST_GOOD_BALANCE is not None:
        return {**_LAST_GOOD_BALANCE, "stale": True,
                "balance_error": "KIS 잔고 장애 — 마지막 정상값 표시"}
    return {"holdings": [], "summary": {}, "as_of": now_iso,
            "stale": False, "balance_error": fetch_error or "KIS 잔고 조회 실패"}


def _ledger_owners_by_code() -> dict[str, list[dict]]:
    """모든 trades(success=1)를 source별 BotLedger로 재계산 → 종목별 귀속 목록.

    KIS 장애와 무관하게 동작해야 한다 — DB만 사용.
    phantom-cleanup/orphan-assign 특수 행 처리는 BotLedger가 담당.
    """
    from src.bots.ledger import BotLedger
    from src.db import get_db

    trades = get_db().get_trades(limit=50000)
    by_source: dict[str, list[dict]] = {}
    for t in trades:
        src = t.get("source") or ""
        if not src:
            continue
        by_source.setdefault(src, []).append(t)

    owners: dict[str, list[dict]] = {}
    for src in sorted(by_source):
        ledger = BotLedger.from_trades(budget=0, trades=by_source[src])
        for p in ledger.positions:
            owners.setdefault(p["code"], []).append({
                "source": src,
                "qty": int(p["qty"]),
                "avg_price": int(p.get("avg_price", 0) or 0),
            })
    return owners


@router.get("/holdings/overview")
async def holdings_overview():
    """보유 종합 상황판 — KIS 실잔고와 봇 장부(BotLedger) 귀속을 나란히 비교.

    장애 대응 3단계 (KIS 플래핑 실사고 반영):
      1) 의심-빈-응답 판정: 장부 보유가 있는데 KIS가 비면 truth로 쓰지 않음
      2) 재시도 1회
      3) 그래도 실패면 마지막 정상값 캐시를 stale=true로 서빙,
         캐시도 없으면 ok=false + 장부 데이터만 반환
    mismatch = kis_qty - ledger_qty — 양수면 장부 미귀속 수량 존재, 음수면
    장부 과잉(봇이 존재하지 않는 주식을 자기 것이라 주장). 0이 정상.
    """
    owners_by_code = _ledger_owners_by_code()
    ledger_total_qty = sum(o["qty"] for lst in owners_by_code.values() for o in lst)

    res = _get_balance_resilient(ledger_total_qty)
    stale = res["stale"]
    balance_error = res["balance_error"]
    kis_rows = res["holdings"]
    cash = int((res["summary"] or {}).get("cash", 0) or 0)
    as_of = res["as_of"]

    from src.kis.master import stock_name_by_code

    holdings: list[dict] = []
    seen: set[str] = set()
    for h in kis_rows:
        code = h.get("stock_code", "")
        kis_qty = int(h.get("quantity", 0) or 0)
        if not code or kis_qty <= 0:
            continue
        seen.add(code)
        code_owners = owners_by_code.get(code, [])
        ledger_qty = sum(o["qty"] for o in code_owners)
        holdings.append({
            "code": code,
            "name": h.get("name") or stock_name_by_code(code),
            "kis_qty": kis_qty,
            "avg_price": int(h.get("avg_price", 0) or 0),
            "current_price": int(h.get("current_price", 0) or 0),
            "eval_amount": int(h.get("eval_amount", 0) or 0),
            "profit_amount": int(h.get("profit_amount", 0) or 0),
            "profit_rate": float(h.get("profit_rate", 0) or 0),
            "owners": code_owners,
            "ledger_qty": ledger_qty,
            "mismatch": kis_qty - ledger_qty,
        })

    # KIS에 없는데 장부에만 있는 종목 — 장부 과잉(음수 mismatch)으로 표시
    for code in sorted(owners_by_code):
        if code in seen:
            continue
        code_owners = owners_by_code[code]
        ledger_qty = sum(o["qty"] for o in code_owners)
        if ledger_qty <= 0:
            continue
        cost = sum(o["qty"] * o.get("avg_price", 0) for o in code_owners)
        holdings.append({
            "code": code,
            "name": stock_name_by_code(code),
            "kis_qty": 0,
            "avg_price": cost // ledger_qty,
            "current_price": 0,
            "eval_amount": 0,
            "profit_amount": 0,
            "profit_rate": 0.0,
            "owners": code_owners,
            "ledger_qty": ledger_qty,
            "mismatch": -ledger_qty,
        })

    return {
        # ok=false는 "표시할 실계좌 정보가 전혀 없음" (캐시 서빙은 ok=true + stale)
        "ok": balance_error is None or stale,
        "stale": stale,
        "as_of": as_of,
        "cash": cash,
        "holdings": holdings,
        "totals": {
            "eval_amount": sum(h["eval_amount"] for h in holdings),
            "profit_amount": sum(h["profit_amount"] for h in holdings),
        },
        "balance_error": balance_error,
    }


def _fetch_benchmark_daily(code: str, count: int) -> list[dict]:
    """KIS 일봉 시세 조회 — 테스트에서 monkeypatch 가능하도록 분리."""
    from src.kis.market import KISMarket
    m = KISMarket()
    try:
        return m.get_daily_price(code, period="D", count=count) or []
    finally:
        try:
            m.close()
        except Exception:
            pass


def _fetch_benchmark_quote(code: str) -> dict | None:
    """KIS 현재가 조회 — 장중 당일 벤치마크 표시용."""
    from src.kis.market import KISMarket
    m = KISMarket()
    try:
        return m.get_price(code)
    finally:
        try:
            m.close()
        except Exception:
            pass


def _fetch_daily_closes(code: str, count: int) -> list[dict]:
    """단일 종목 일봉 — 테스트 monkeypatch 지점."""
    from src.kis.market import KISMarket
    m = KISMarket()
    try:
        return m.get_daily_price(code, period="D", count=count) or []
    finally:
        try:
            m.close()
        except Exception:
            pass


def _build_close_table(codes: list[str], days: int) -> dict:
    """{(code, 'YYYY-MM-DD'): close} 캐시. 코드별 1회 조회.

    get_daily_price의 date는 'YYYYMMDD' → 'YYYY-MM-DD'로 정규화.
    조회 실패(네트워크/장애)는 무시하고 cost-basis 폴백에 맡긴다.
    """
    table: dict = {}
    count = max(2, days + 3)  # 주말·여유분
    for code in set(c for c in codes if c):
        try:
            chart = _fetch_daily_closes(code, count)
        except Exception:
            chart = []
        for row in chart:
            raw = str(row.get("date") or "")
            if len(raw) == 8:
                norm = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
            else:
                norm = raw
            close = int(row.get("close") or 0)
            if norm and close > 0:
                table[(code, norm)] = close
    return table


def _benchmark_daily_returns(code: str, days: int) -> dict[str, dict]:
    """{YYYY-MM-DD: {return_pct, source}} — 일봉 + 당일 현재가."""
    from datetime import datetime

    # 종가-전일종가 계산을 위해 +1일 여유.
    chart = _fetch_benchmark_daily(code, count=max(2, days + 1))
    if len(chart) < 2:
        out: dict[str, dict] = {}
    else:
        # 일자 오름차순으로 정렬 (KIS는 최신부터 내려옴)
        sorted_chart = sorted(chart, key=lambda r: r.get("date", ""))
        out = {}
        prev_close = None
        for row in sorted_chart:
            raw = row.get("date") or ""
            close = int(row.get("close", 0) or 0)
            if not raw or close <= 0:
                prev_close = close if close > 0 else prev_close
                continue
            iso = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) == 8 else raw
            if prev_close and prev_close > 0:
                out[iso] = {
                    "return_pct": round((close - prev_close) / prev_close * 100, 3),
                    "source": "daily",
                }
            prev_close = close

    today = datetime.now().strftime("%Y-%m-%d")
    if today not in out:
        try:
            quote = _fetch_benchmark_quote(code) or {}
            change_rate = _to_float(quote.get("change_rate"), 0)
            price = int(_to_float(quote.get("price"), 0))
        except Exception:
            quote = {}
            change_rate = 0
            price = 0
        if quote and price > 0:
            out[today] = {
                "return_pct": round(change_rate, 3),
                "source": "realtime",
                "close": price,
            }
    return out


def _iso_from_chart_date(raw: str) -> str:
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) == 8 else raw


def _chart_raw_date(iso: str) -> str:
    return iso.replace("-", "") if iso else ""


def _latest_chart_iso(chart: list[dict]) -> str:
    if not chart:
        return ""
    latest = max((str(r.get("date") or "") for r in chart), default="")
    return _iso_from_chart_date(latest)


def _benchmark_return_pct(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return _to_float(value.get("return_pct"), 0)
    return _to_float(value, 0)


def _benchmark_period_anchor(code: str, days: int) -> dict:
    """기간 anchor: 시작가 → 끝가 복리 수익률. '예산을 풀투입했으면' 비교용.

    Returns:
        {start_close, end_close, start_date, end_date, period_return_pct}
        조회 실패 시 {} (또는 partial).
    """
    chart = _fetch_benchmark_daily(code, count=max(2, days + 1))
    if not chart or len(chart) < 2:
        return {}
    sorted_chart = sorted(chart, key=lambda r: r.get("date", ""))
    first = sorted_chart[0]
    last = sorted_chart[-1]
    start_close = int(first.get("close", 0) or 0)
    end_close = int(last.get("close", 0) or 0)
    raw_e = last.get("date") or ""
    try:
        quote = _fetch_benchmark_quote(code) or {}
        today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        if quote and _iso_from_chart_date(raw_e) < today:
            quote_price = int(_to_float(quote.get("price"), 0))
            if quote_price > 0:
                end_close = quote_price
                raw_e = _chart_raw_date(today)
    except Exception:
        pass
    if start_close <= 0 or end_close <= 0:
        return {}
    pct = (end_close - start_close) / start_close * 100
    raw_s = first.get("date") or ""
    return {
        "start_close": start_close,
        "end_close": end_close,
        "start_date": _iso_from_chart_date(raw_s),
        "end_date": _iso_from_chart_date(raw_e),
        "period_return_pct": round(pct, 3),
    }


def _benchmark_period_anchor_range(
    chart: list[dict],
    start_iso: str,
    end_iso: str,
    today_quote_price: int | None = None,
) -> dict:
    """봇 거래 구간([start_iso, end_iso])에 맞춰 벤치마크 복리 수익률을 계산.

    chart는 _fetch_benchmark_daily()의 사전 조회 결과를 재사용.
    start_iso, end_iso는 봇의 첫/마지막 거래일 (YYYY-MM-DD).
    봇 마지막 거래일이 오늘이면 today_quote_price(장중가)로 끝값을 갱신.
    """
    if not chart:
        return {}
    sorted_chart = sorted(chart, key=lambda r: r.get("date", ""))
    in_range = [
        r for r in sorted_chart
        if start_iso <= _iso_from_chart_date(r.get("date") or "") <= end_iso
    ]
    if not in_range:
        return {}
    first = in_range[0]
    last = in_range[-1]
    start_close = int(first.get("close", 0) or 0)
    end_close = int(last.get("close", 0) or 0)
    raw_e = last.get("date") or ""
    today_iso = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    if (
        today_quote_price
        and today_quote_price > 0
        and end_iso >= today_iso
        and _iso_from_chart_date(raw_e) < today_iso
    ):
        end_close = today_quote_price
        raw_e = _chart_raw_date(today_iso)
    if start_close <= 0 or end_close <= 0:
        return {}
    pct = (end_close - start_close) / start_close * 100
    return {
        "start_close": start_close,
        "end_close": end_close,
        "start_date": _iso_from_chart_date(first.get("date") or ""),
        "end_date": _iso_from_chart_date(raw_e),
        "period_return_pct": round(pct, 3),
    }


@router.get("/bot-performance/daily")
async def bots_daily_performance(days: int = 14, pipe_id: str = ""):
    """봇별 일간 수익률 — 어떤 봇이 잘하나 비교용.

    각 봇의 source 단위로 (날짜, 봇)별 realized_pnl 합계를 계산.
    phantom-cleanup 합성 행은 제외. buy는 PnL=0이라 자연스럽게 win/loss 카운트 안 됨.

    Args:
        days: 조회 기간 (1~90)
        pipe_id: 특정 파이프로 필터 (빈 값이면 전체)

    Returns:
        {period_days, bots: [{bot_id, budget, totals, days: [{date, pnl, ...}]}]}
    """
    from src.db import get_db
    from src.strategy import StrategyManager
    from datetime import datetime, timedelta

    days = max(1, min(int(days or 14), 90))
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    db = get_db()

    where_extra = "AND pipe_id = ?" if pipe_id else ""
    params = [cutoff]
    if pipe_id:
        params.append(pipe_id)

    rows = db._conn.execute(f"""
        SELECT
            source AS bot_id,
            DATE(timestamp) AS day,
            SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END) AS trades,
            SUM(CASE WHEN side='sell' AND realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN side='sell' AND realized_pnl < 0 THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN side='sell' THEN realized_pnl ELSE 0 END) AS pnl
        FROM trades
        WHERE source != ''
          AND success = 1
          AND (reason IS NULL OR reason != 'phantom-cleanup')
          AND timestamp >= ?
          {where_extra}
        GROUP BY source, day
        ORDER BY source, day DESC
    """, params).fetchall()

    # 봇별 예산 (StrategyManager에서 ratio×budget 합산)
    bot_budget: dict[str, int] = {}
    try:
        mgr = StrategyManager()
        for s in mgr.list_strategies():
            if pipe_id and s.id != pipe_id:
                continue
            for b in (s.bots or []):
                bid = b.get("bot_id", "")
                if not bid:
                    continue
                bot_budget[bid] = bot_budget.get(bid, 0) + int(s.budget * float(b.get("ratio", 0) or 0))
    except Exception:
        pass

    per_bot: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        bid = d["bot_id"]
        entry = per_bot.setdefault(bid, {
            "bot_id": bid,
            "budget": bot_budget.get(bid, 0),
            "totals": {"pnl": 0, "trades": 0, "wins": 0, "losses": 0},
            "days": [],
        })
        pnl = int(d["pnl"] or 0)
        trades = int(d["trades"] or 0)
        wins = int(d["wins"] or 0)
        losses = int(d["losses"] or 0)
        day_row = {
            "date": d["day"],
            "pnl": pnl,
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "roi_pct": None,  # equity 기반으로 아래에서 채움
        }
        entry["days"].append(day_row)
        entry["totals"]["pnl"] += pnl
        entry["totals"]["trades"] += trades
        entry["totals"]["wins"] += wins
        entry["totals"]["losses"] += losses

    # ── equity 기반 일별 수익률·TWR 재구성 (자본 규모 무관 공정 비교) ──
    from src.services.bot_equity import reconstruct_daily_equity, period_return

    all_trades = [t for t in db.get_trades(limit=10000)
                  if t.get("success", True) and t.get("source")]
    by_src: dict[str, list] = {}
    for t in all_trades:
        by_src.setdefault(t["source"], []).append(t)

    held_codes = {t.get("stock_code") for t in all_trades if t.get("stock_code")}
    close_table = _build_close_table(list(held_codes), days)
    close_fn = lambda code, date: close_table.get((code, date))

    for bid, entry in per_bot.items():
        day_dates = sorted(d["date"] for d in entry["days"] if d.get("date"))
        eq_rows = reconstruct_daily_equity(
            by_src.get(bid, []), entry["budget"], day_dates, close_fn)
        eq_by_date = {r["date"]: r for r in eq_rows}
        for d in entry["days"]:
            er = eq_by_date.get(d["date"])
            d["roi_pct"] = (round(er["return_pct"], 3)
                            if er and er["return_pct"] is not None else None)
            d["equity_start"] = er["equity_start"] if er else 0
        pr = period_return(eq_rows)
        t = entry["totals"]
        t["twr_pct"] = round(pr["twr_pct"], 3)
        t["cum_pnl"] = pr["cum_pnl"]
        t["avg_equity"] = pr["avg_equity"]
        t["price_estimated_days"] = sum(
            1 for r in eq_rows if r["price_estimated"])

    # 벤치마크 일간 수익률 + 기간 anchor (둘 다 표시 — % 기준 & 투자금 기준).
    # 일간: 각 날짜의 close-to-close 변동 — 시각화·차트용
    # 기간 anchor (전체): 화면 상단 KODEX 200 카드용 — days 풀 기간 표시
    # 기간 anchor (봇별): alpha 비교용 — 봇 첫 거래일~마지막 거래일 구간으로 정렬
    try:
        bench_by_date = _benchmark_daily_returns(BENCHMARK_CODE, days)
    except Exception as e:
        logger.warning("benchmark daily fetch 실패: %s", e)
        bench_by_date = {}
    try:
        bench_period = _benchmark_period_anchor(BENCHMARK_CODE, days)
    except Exception as e:
        logger.warning("benchmark period fetch 실패: %s", e)
        bench_period = {}
    bench_period_pct = float(bench_period.get("period_return_pct", 0) or 0)

    # 봇별 구간 정렬용 — 한 번만 조회해서 재사용
    try:
        bench_chart_for_range = _fetch_benchmark_daily(
            BENCHMARK_CODE, count=max(2, days + 1)
        )
    except Exception as e:
        logger.warning("benchmark range chart fetch 실패: %s", e)
        bench_chart_for_range = []
    bench_quote_price: int = 0
    try:
        bench_quote = _fetch_benchmark_quote(BENCHMARK_CODE) or {}
        bench_quote_price = int(_to_float(bench_quote.get("price"), 0))
    except Exception:
        bench_quote_price = 0

    bots = []
    for bid, entry in per_bot.items():
        t = entry["totals"]
        decided = t["wins"] + t["losses"]
        t["win_rate"] = round(t["wins"] / decided, 3) if decided > 0 else 0.0
        # 기간 대표 수익률 = equity 기반 시간가중 복리수익률(TWR)
        t["roi_pct"] = t.get("twr_pct", 0.0)

        # 일별 alpha — 봇 활성일 한정 일간 비교 (시각화·세부)
        bench_total = 0.0
        bench_days = 0
        for d in entry["days"]:
            br = _benchmark_return_pct(bench_by_date.get(d["date"]))
            if br is None or d.get("roi_pct") is None:
                d["alpha_pct"] = None
            else:
                d["alpha_pct"] = round(d["roi_pct"] - br, 3)
                bench_total += br
                bench_days += 1
        if bench_days > 0:
            t["benchmark_pct_sum"] = round(bench_total, 3)
            t["alpha_pct_sum"] = round(t["roi_pct"] - bench_total, 3)

        # 기간 anchor — 봇 거래 구간(첫 거래일 ~ 마지막 거래일)로 정렬해서 계산.
        # 봇이 일부 기간만 가동했을 때 풀 기간 KODEX와 비교하는 왜곡을 제거.
        bot_dates = [d["date"] for d in entry["days"] if d.get("date")]
        if bot_dates:
            bot_start = min(bot_dates)
            bot_end = max(bot_dates)
            t["bot_first_date"] = bot_start
            t["bot_last_date"] = bot_end
            t["bot_trading_days"] = len(set(bot_dates))
            bench_for_bot = _benchmark_period_anchor_range(
                bench_chart_for_range, bot_start, bot_end, bench_quote_price
            )
            bot_bench_pct = float(bench_for_bot.get("period_return_pct", 0) or 0)
            t["benchmark_period_start_date"] = bench_for_bot.get("start_date")
            t["benchmark_period_end_date"] = bench_for_bot.get("end_date")
        else:
            bot_bench_pct = bench_period_pct  # 거래 없으면 풀 기간으로 fallback
        budget = entry["budget"] or 0
        bench_value_krw = int(round(budget * bot_bench_pct / 100))
        t["benchmark_period_pct"] = round(bot_bench_pct, 3)
        t["benchmark_period_value"] = bench_value_krw  # 봇 구간 동안 KODEX 풀투입 가상 손익(KRW)
        t["alpha_pct"] = round(t["roi_pct"] - bot_bench_pct, 3)
        t["alpha_krw"] = int(t["pnl"]) - bench_value_krw  # 봇 실현 − 같은 구간 KODEX 평가
        bots.append(entry)
    bots.sort(key=lambda b: b["totals"].get("twr_pct", 0), reverse=True)

    # 벤치마크 일자별 (UI 표시용, 내림차순)
    bench_days_sorted = sorted(
        [
            {
                "date": k,
                "return_pct": _benchmark_return_pct(v) or 0,
                "source": v.get("source", "daily") if isinstance(v, dict) else "daily",
            }
            for k, v in bench_by_date.items()
        ],
        key=lambda x: x["date"], reverse=True,
    )

    return {
        "period_days": days,
        "bots": bots,
        "benchmark": {
            "code": BENCHMARK_CODE,
            "name": BENCHMARK_NAME,
            "days": bench_days_sorted,
            # 일간 합 (참고용 — 봇 활성일자만 합산하지 않은 raw 일간 합)
            "total_pct": round(sum(_benchmark_return_pct(v) or 0 for v in bench_by_date.values()), 3),
            # 기간 복리 (진짜 기회비용 비교 기준)
            "period_return_pct": round(bench_period_pct, 3),
            "start_close": bench_period.get("start_close"),
            "end_close": bench_period.get("end_close"),
            "start_date": bench_period.get("start_date"),
            "end_date": bench_period.get("end_date"),
        },
    }


# ── 일일 손익 ──

@router.get("/pnl/daily")
async def get_daily_pnl(date: str = ""):
    """봇(source)별 당일 실현손익 — BotLedger FIFO 매칭 기준.

    해당일 실현손익 = ledger(해당일 종료 시점) − ledger(해당일 시작 시점).
    FIFO가 과거 매수 lot과 당일 매도를 정확히 짝짓고, phantom-cleanup
    합성 행은 BotLedger가 자동 제외한다. 당일 거래가 있는 source만 응답.
    평가손익은 미포함 — UI가 /api/account/balance 보유 데이터로 합산.

    Args:
        date: YYYY-MM-DD (미지정 시 오늘 KST)

    Returns:
        {date, bots: [{source, realized, buys, sells}], total_realized}
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from src.bots.ledger import BotLedger
    from src.db import get_db
    from src.services.bot_equity import reconstruct_daily_equity
    from src.strategy import StrategyManager

    if not date:
        date = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return {"error": f"date 형식은 YYYY-MM-DD: {date}",
                "date": date, "bots": [], "total_realized": 0}

    trades = get_db().get_trades(limit=10000)
    by_source: dict[str, list[dict]] = {}
    for t in trades:
        if not t.get("success", True):
            continue
        src = t.get("source") or ""
        if not src:
            continue
        by_source.setdefault(src, []).append(t)

    # 봇별 allocated budget — equity 현금 anchor용
    bot_budget: dict[str, int] = {}
    try:
        for s in StrategyManager().list_strategies():
            for b in (s.bots or []):
                bid = b.get("bot_id", "")
                if bid:
                    bot_budget[bid] = bot_budget.get(bid, 0) + int(
                        s.budget * float(b.get("ratio", 0) or 0))
    except Exception:
        pass

    # 당일 equity 평가용 종가 테이블 (당일 보유 가능 코드)
    held = {t.get("stock_code") for t in trades if t.get("stock_code")}
    close_table = _build_close_table(list(held), days=2)
    close_fn = lambda code, dt: close_table.get((code, dt))

    bots = []
    total = 0
    for src, rows in by_source.items():
        upto = [t for t in rows if (t.get("timestamp") or "")[:10] <= date]
        before = [t for t in rows if (t.get("timestamp") or "")[:10] < date]
        realized = (BotLedger.from_trades(0, upto).realized_pnl
                    - BotLedger.from_trades(0, before).realized_pnl)
        # 합성 행은 건수에서 제외 — phantom-cleanup SELL, orphan-assign BUY는
        # 실거래가 아니다 (realized는 BotLedger가 이미 영향 없이 처리)
        day = [t for t in rows
               if (t.get("timestamp") or "")[:10] == date
               and (t.get("reason") or "") not in ("phantom-cleanup",
                                                   "orphan-assign")]
        buys = sum(1 for t in day if t.get("side") == "buy")
        sells = sum(1 for t in day if t.get("side") == "sell")
        if buys + sells == 0 and realized == 0:
            continue  # 당일 거래 없는 source는 제외
        # 당일 equity 기준 수익률 (자본 규모 무관 공정 비교)
        eq = reconstruct_daily_equity(rows, bot_budget.get(src, 0), [date], close_fn)
        ret = eq[0]["return_pct"] if eq else None
        bots.append({"source": src, "realized": realized,
                     "return_pct": round(ret, 3) if ret is not None else None,
                     "buys": buys, "sells": sells})
        total += realized

    # 수익률(%) 내림차순 — None은 맨 뒤로
    bots.sort(key=lambda b: (b["return_pct"] is not None, b["return_pct"] or 0),
              reverse=True)
    return {"date": date, "bots": bots, "total_realized": total}


@router.get("/news/ai-config")
async def get_news_ai_config():
    """뉴스 AI 분류기 설정 조회."""
    from src.news.ai_classifier import load_news_config
    return load_news_config()


@router.post("/news/ai-config")
async def set_news_ai_config(body: dict):
    """뉴스 AI 분류기 설정 변경.

    유효 키: ai_enabled, provider, model, fallback_to_regex,
            ai_fallback_enabled, timeout_seconds, include_affected_stocks.
    """
    from src.news.ai_classifier import load_news_config, save_news_config
    valid_keys = {"ai_enabled", "provider", "model", "fallback_to_regex",
                  "ai_fallback_enabled", "timeout_seconds",
                  "include_affected_stocks"}
    cfg = load_news_config()
    for k, v in body.items():
        if k in valid_keys:
            cfg[k] = v
    save_news_config(cfg)
    return {"success": True, **cfg}


@router.get("/ai/models")
async def list_ai_models(provider: str = ""):
    """AI 프로바이더와 모델 카탈로그.

    - provider 미지정: 전체 프로바이더 + 모델 목록을 반환
    - provider 지정: 해당 프로바이더의 모델 목록만 반환
    """
    from src.bots.ai_caller import _get_llm_client
    client = _get_llm_client()
    if provider:
        models = client.list_models(provider)
        return {"provider": provider, "models": models}
    providers = client.list_providers()
    return {
        "providers": [
            {**p, "models": client.list_models(p["id"])} for p in providers
        ],
    }


@router.get("/bots/{bot_id}/ai-config")
async def get_ai_config(bot_id: str):
    """봇의 AI 프로바이더/모델 설정 조회."""
    from src.bots.safeguards import load_bot_config
    cfg = load_bot_config(bot_id)
    return {
        "bot_id": bot_id,
        "ai_provider": cfg.get("ai_provider", "codex"),
        "ai_model": cfg.get("ai_model", ""),
    }


@router.post("/bots/{bot_id}/ai-config")
async def set_ai_config(bot_id: str, body: dict):
    """봇의 AI 프로바이더/모델 설정 변경. 파이프 재시작 없이 다음 호출부터 적용."""
    from src.bots.safeguards import load_bot_config, save_bot_config
    valid_providers = ["codex", "claude", "copilot"]
    provider = body.get("ai_provider", "")
    model = body.get("ai_model", "")

    if provider and provider not in valid_providers:
        return {"success": False, "error": f"지원 프로바이더: {valid_providers}"}

    cfg = load_bot_config(bot_id)
    if provider:
        cfg["ai_provider"] = provider
    if "ai_model" in body:
        cfg["ai_model"] = model
    save_bot_config(bot_id, cfg)

    return {
        "success": True,
        "bot_id": bot_id,
        "ai_provider": cfg.get("ai_provider", "codex"),
        "ai_model": cfg.get("ai_model", ""),
    }


# ── 봇 수동 판단 요청 ──

@router.post("/bots/{bot_id}/judge")
async def manual_judge(bot_id: str):
    """봇에게 수동으로 1회 AI 판단을 요청합니다."""
    import asyncio
    from src.bots.factory import create_bot
    from src.strategy import StrategyManager

    mgr = StrategyManager()
    # 파이프에서 해당 봇의 예산 찾기
    budget = 0
    pipe_id = ""
    for s in mgr.list_strategies():
        for b in s.bots:
            if b.get("bot_id") == bot_id:
                budget = int(s.budget * b.get("ratio", 0))
                pipe_id = s.id
                break

    instance_id = f"{pipe_id}:{bot_id}" if pipe_id else bot_id
    bot = create_bot(bot_id, budget, instance_id)
    if not bot:
        return {"success": False, "error": f"봇 '{bot_id}' 생성 실패"}

    # _collect_data + _build_codex_prompt + _invoke_codex + _parse_decision
    try:
        loop = asyncio.get_event_loop()
        context = await loop.run_in_executor(None, bot._collect_data)
        prompt = bot._build_codex_prompt(context)
        output = await loop.run_in_executor(None, bot._invoke_codex, prompt)
        decision = bot._parse_decision(output)

        return {
            "success": True,
            "bot_id": bot_id,
            "decision": decision,
            "raw_output_length": len(output) if output else 0,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── 시장 시간 ──

@router.get("/market-session")
async def market_session():
    """현재 시장 세션 정보."""
    from src.bots.safeguards import get_market_session
    return get_market_session()


# ── ETF 시그널 ──

@router.get("/etf-signals")
async def etf_signals():
    """ETF별 뉴스 감성 + 방향 시그널."""
    from src.services.etf_signal import get_etf_signals
    return {"signals": get_etf_signals()}


# ── 산업 지수 ──

@router.get("/industry-index")
async def industry_index(sector: str = ""):
    """산업별 종합 지수. sector 파라미터로 특정 섹터 필터."""
    from src.services.industry_index import get_industry_index
    indices = get_industry_index()
    if sector:
        indices = {k: v for k, v in indices.items() if k == sector}
    return {"indices": indices}


@router.get("/industry-index/history")
async def industry_index_history(sector: str, sub_sector: str = "", hours: int = 24):
    """산업 지수 시계열 이력."""
    from src.services.industry_index import get_index_history
    history = get_index_history(sector=sector, sub_sector=sub_sector, hours=hours)
    return {"history": history, "sector": sector, "sub_sector": sub_sector}


# ── 게시판 ──

@router.get("/board")
async def get_board(pipe_id: str = "", limit: int = 30):
    """파이프 게시판 조회."""
    from src.db import get_db
    posts = get_db().get_board(pipe_id, limit)
    return {"posts": posts}


@router.post("/board")
async def post_to_board(body: dict):
    """게시판에 글 등록."""
    from src.db import get_db
    required = ["bot_id", "type", "content"]
    for key in required:
        if key not in body:
            return {"success": False, "error": f"'{key}' 필수"}
    post_id = get_db().post_board(
        pipe_id=body.get("pipe_id", ""),
        bot_id=body["bot_id"],
        type=body["type"],
        content=body["content"],
        code=body.get("code", ""),
        extra=body.get("extra", ""),
    )
    return {"success": True, "id": post_id}


@router.get("/decisions")
async def get_decisions(count: int = 50, pipe_id: str = "", bot_id: str = ""):
    db = get_db()
    decisions = db.get_decisions(pipe_id=pipe_id, limit=count)
    if bot_id:
        decisions = [d for d in decisions if bot_id in (d.get("action_taken", "") or "")]
    for d in decisions:
        # UI 호환 필드. DB의 원본 판단 사유는 output_summary에 저장된다.
        summary = d.get("output_summary", "") or ""
        d.setdefault("detail", summary)
        d.setdefault("reason", summary)
        d.setdefault("source", d.get("bot_id", ""))
    return {"decisions": decisions}


# ── 뉴스 ──

@router.get("/news")
async def get_news(count: int = 30, source: str = ""):
    db = get_db()
    return {"news": db.get_news(count=count, source=source)}


@router.get("/news/search")
async def search_news(q: str, count: int = 20):
    db = get_db()
    return {"news": db.search_news(keyword=q, count=count)}


@router.get("/news/enriched")
async def get_news_enriched(category: str = "", source: str = "",
                            sentiment: str = "", count: int = 30, offset: int = 0):
    """필터 기반 뉴스 조회 (카테고리/소스/감성)."""
    db = get_db()
    return {"news": db.get_news_filtered(category=category, source=source,
                                         sentiment=sentiment, count=count, offset=offset)}


@router.get("/news/semantic")
async def semantic_search_news(q: str, count: int = 10):
    """시맨틱 뉴스 검색."""
    from src.news.search import get_search_engine
    results = get_search_engine().hybrid_search(query=q, top_k=count)
    return {"news": results, "mode": "semantic"}


@router.get("/news/stats")
async def news_stats():
    """뉴스 통계."""
    return get_db().get_news_stats()


@router.get("/news/categories")
async def news_categories():
    """사용 가능한 카테고리 목록."""
    from src.news.classifier import CATEGORIES
    return {"categories": list(CATEGORIES.keys()) + ["기타"]}


# ── 매매 이력 (DB) ──

from src.db import get_db


@router.get("/trades")
async def get_trades(pipe_id: str = "", start: str = "", end: str = "",
                     side: str = "", source: str = "", limit: int = 100):
    db = get_db()
    return {"trades": db.get_trades(pipe_id=pipe_id, start=start, end=end, side=side, source=source, limit=limit)}


@router.get("/trades/summary")
async def get_trades_summary(pipe_id: str = ""):
    db = get_db()
    return db.trade_summary(pipe_id=pipe_id)


@router.get("/snapshots")
async def get_snapshots(pipe_id: str = "", days: int = 7):
    db = get_db()
    return {"snapshots": db.get_snapshots(pipe_id=pipe_id, days=days)}


# ── SSE 스트림 ──

@router.get("/events")
async def sse_stream(request: Request):
    queue = broker.subscribe()

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=30)
                    yield broker.format_sse(entry)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            broker.unsubscribe(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ── 이벤트 히스토리 ──

@router.get("/events/history")
async def get_event_history():
    return {"events": broker.history}

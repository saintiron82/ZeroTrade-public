"""Wall Street US AI — 미국주식 전용 AI 에이전트 봇.

AI가 후보와 방향을 판단하되, 주문 전에는 코드 기반 리스크 가드가 최종 차단한다.
국내 ETF 파이프 장부와 해외주식 장부가 섞이지 않도록 미국 포지션은 봇 상태에 별도 저장한다.
"""

import asyncio
import json
import logging
import math
import re
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.bots.base import BotBase, BotConfig
from src.bots.safeguards import DailyTradeLimit, kst_now, load_bot_config, write_bot_log
from src.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

NY_TZ = ZoneInfo("America/New_York")

DEFAULT_WATCHLIST = [
    {"symbol": "NVDA", "exchange": "NAS", "name": "NVIDIA", "bucket": "AI 반도체"},
    {"symbol": "MSFT", "exchange": "NAS", "name": "Microsoft", "bucket": "AI 소프트웨어"},
    {"symbol": "AAPL", "exchange": "NAS", "name": "Apple", "bucket": "메가캡"},
    {"symbol": "AMZN", "exchange": "NAS", "name": "Amazon", "bucket": "메가캡"},
    {"symbol": "META", "exchange": "NAS", "name": "Meta", "bucket": "AI/광고"},
    {"symbol": "GOOGL", "exchange": "NAS", "name": "Alphabet", "bucket": "AI/광고"},
    {"symbol": "TSLA", "exchange": "NAS", "name": "Tesla", "bucket": "고변동성"},
    {"symbol": "AVGO", "exchange": "NAS", "name": "Broadcom", "bucket": "AI 반도체"},
    {"symbol": "AMD", "exchange": "NAS", "name": "AMD", "bucket": "AI 반도체"},
    {"symbol": "QQQ", "exchange": "NAS", "name": "Invesco QQQ", "bucket": "지수 ETF"},
    {"symbol": "SPY", "exchange": "NYS", "name": "SPDR S&P 500", "bucket": "지수 ETF"},
    {"symbol": "SOXL", "exchange": "AMS", "name": "Direxion SOXL", "bucket": "레버리지 ETF"},
    {"symbol": "TQQQ", "exchange": "NAS", "name": "ProShares TQQQ", "bucket": "레버리지 ETF"},
]

DEFAULT_PARAMS = {
    "interval_seconds": 600,
    "confidence_threshold": 0.62,
    "max_positions": 2,
    "max_daily_trades": 5,
    "max_position_pct": 0.45,
    "allow_min_one_share": True,
    "min_order_usd": 20.0,
    "fx_rate": 1400.0,
    "take_profit_pct": 4.5,
    "trailing_stop_pct": -2.2,
    "hard_stop_pct": -3.0,
    "max_hold_days": 3,
    "allow_extended_hours": False,
    "order_type": "LIMIT",
    "limit_slippage_pct": 0.25,
    "risk_reward_min": 1.5,
    "codex_timeout": 180,
    "ai_provider": "codex",
    "ai_model": "",
    "watchlist": DEFAULT_WATCHLIST,
}


class WallStreetUSBot(BotBase):
    """미국주식 AI 에이전트 + 규칙형 집행 가드."""

    BOT_ID = "wallstreet-us-ai"

    def __init__(self, config: BotConfig | dict = None):
        self._params = {**DEFAULT_PARAMS}
        self._params.update(load_bot_config(self.BOT_ID))
        self._watchlist = self._normalize_watchlist(self._params.get("watchlist", DEFAULT_WATCHLIST))
        self._positions: list[dict] = []
        self._last_analysis: dict = {}
        self._debate_log: list[dict] = []
        self._last_validation: dict = {}
        self._last_preflight: dict = {}
        self._daily_limit = DailyTradeLimit(max_trades=int(self._params["max_daily_trades"]))

        if isinstance(config, dict):
            config.setdefault("interval_seconds", self._params["interval_seconds"])
        elif config is None:
            config = BotConfig(interval_seconds=self._params["interval_seconds"])
        else:
            if config.interval_seconds in (300, 1800, 3600):
                config.interval_seconds = int(self._params["interval_seconds"])

        super().__init__(config)
        logger.info(
            "[%s] 초기화 — 미국주식 AI, %ds 간격, confidence >= %.2f, watchlist=%d",
            self.BOT_ID,
            self._config.interval_seconds,
            self._params["confidence_threshold"],
            len(self._watchlist),
        )

    @staticmethod
    def _normalize_watchlist(raw: list) -> list[dict]:
        normalized = []
        for item in raw or []:
            if isinstance(item, str):
                normalized.append({"symbol": item.upper(), "exchange": "NAS", "name": item.upper(), "bucket": ""})
            elif isinstance(item, dict) and item.get("symbol"):
                normalized.append({
                    "symbol": item["symbol"].upper(),
                    "exchange": item.get("exchange", "NAS").upper(),
                    "name": item.get("name", item["symbol"].upper()),
                    "bucket": item.get("bucket", ""),
                })
        return normalized

    @property
    def _watch_map(self) -> dict[str, dict]:
        return {w["symbol"]: w for w in self._watchlist}

    def _load_state(self):
        """미국 포지션은 국내 파이프 holdings와 reconcile하지 않는다."""
        try:
            from src.db import get_db
            state = get_db().load_bot_state(self.state_key)
            if not state:
                return
            self._positions = state.get("positions", [])
            self._realized_pnl = int(state.get("realized_pnl", 0))
            self._scan_count = int(state.get("scan_count", 0))
            self._last_analysis = state.get("last_analysis", {})
            self.engine.realized_pnl = self._realized_pnl
            logger.info("[%s] 상태 복원: %d개 US 포지션", self.BOT_ID, len(self._positions))
        except Exception as e:
            logger.debug("[%s] 상태 복원 실패: %s", self.BOT_ID, e)

    def _save_state(self):
        try:
            from src.db import get_db
            get_db().save_bot_state(self.state_key, {
                "positions": self._positions,
                "realized_pnl": self._realized_pnl,
                "scan_count": self._scan_count,
                "last_analysis": self._last_analysis,
            })
        except Exception as e:
            logger.warning("[%s] 상태 저장 실패: %s", self.BOT_ID, e)

    def _reload_params_if_needed(self):
        if self._scan_count % 100 != 0:
            return
        cfg = load_bot_config(self.BOT_ID)
        if not cfg:
            return
        self._params.update(cfg)
        self._watchlist = self._normalize_watchlist(self._params.get("watchlist", DEFAULT_WATCHLIST))
        self._daily_limit.max_trades = int(self._params["max_daily_trades"])
        self._config.interval_seconds = int(self._params["interval_seconds"])

    def _us_session(self) -> dict:
        now_et = datetime.now(NY_TZ)
        weekday = now_et.weekday()
        t = now_et.time()
        is_weekday = weekday < 5
        pre = dtime(4, 0) <= t < dtime(9, 30)
        regular = dtime(9, 30) <= t < dtime(16, 0)
        after = dtime(16, 0) <= t < dtime(20, 0)
        extended = pre or after
        allow_ext = bool(self._params.get("allow_extended_hours", False))
        can_trade = is_weekday and (regular or (allow_ext and extended))
        minutes_to_close = 0
        if is_weekday and t < dtime(16, 0):
            close_dt = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
            minutes_to_close = max(0, (close_dt - now_et).total_seconds() / 60)
        label = "US 정규장" if regular else "US 프리마켓" if pre else "US 애프터마켓" if after else "US 장외"
        return {
            "now_et": now_et,
            "label": label,
            "regular": is_weekday and regular,
            "can_buy": can_trade,
            "can_sell": can_trade,
            "minutes_to_close": minutes_to_close,
        }

    def _collect_data(self) -> str:
        market = self._get_market()
        session = self._us_session()
        rows = []

        for w in self._watchlist:
            symbol = w["symbol"]
            exchange = w["exchange"]
            try:
                price = market.get_us_price(symbol, exchange)
                if not price or price.get("price", 0) <= 0:
                    continue
                daily = market.get_us_daily_price(symbol, exchange, count=25)
                metrics = self._calc_metrics(price, daily)
                affordable_qty = math.floor(self._budget_usd() / float(price.get("price", 0) or 1))
                rows.append({**w, **price, **metrics, "affordable_qty": affordable_qty})
            except Exception as e:
                logger.warning("[%s] US 시세 실패 %s: %s", self.BOT_ID, symbol, e)

        data_path = Path("/tmp/wallstreet_us_ai_data.json")
        try:
            data_path.write_text(json.dumps({
                "timestamp": kst_now().isoformat(),
                "session": {k: str(v) for k, v in session.items()},
                "watchlist": rows,
                "positions": self._positions,
            }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except Exception:
            pass

        market_lines = "\n".join(
            f"  {r['symbol']} [{r['exchange']}] {r.get('name','')}: "
            f"${r['price']:.2f} ({r['change_rate']:+.2f}%) "
            f"5D {r['mom_5d']:+.2f}% 20D {r['mom_20d']:+.2f}% "
            f"vol {r['volume']:,} / avg20 {r['avg_vol_20']:,} "
            f"SMA5/20 {r['sma_5']:.2f}/{r['sma_20']:.2f} "
            f"예산상 {r['affordable_qty']}주"
            for r in sorted(rows, key=lambda x: (x.get("change_rate", 0), x.get("mom_5d", 0)), reverse=True)
        ) or "  (조회 실패)"

        pos_lines = "\n".join(self._format_position_line(p) for p in self._positions) or "  (보유 없음)"
        news_lines = "\n".join(
            f"  [{n.get('category','')}] {n.get('title','')[:70]} ({n.get('sentiment','')}, {n.get('score',0):+})"
            for n in self._get_news_context(count=8)
        ) or "  (뉴스 없음)"
        budget_usd = self._budget_usd()
        actual_buyable = (getattr(self, "_last_preflight", {}) or {}).get("buyable_candidates", [])
        actual_buyable_lines = "\n".join(
            f"  {c['symbol']} [{c['exchange']}]: ${c['price']:.2f}, 실제 {c['qty']}주 가능, 주문가능 ${c.get('amount', 0):,.2f}"
            for c in actual_buyable[:10]
        ) or f"  ({(getattr(self, '_last_preflight', {}) or {}).get('reason', '사전 확인 전')})"

        return f"""[현재 시각]
  KST: {kst_now().strftime("%Y-%m-%d %H:%M:%S")}
  ET: {session['now_et'].strftime("%Y-%m-%d %H:%M:%S")}
  세션: {session['label']} | 매수 {session['can_buy']} | 매도 {session['can_sell']} | 마감까지 {session['minutes_to_close']:.0f}분

[미국 워치리스트 시세]
{market_lines}

[보유 US 포지션]
{pos_lines}

[최근 뉴스]
{news_lines}

[제약 조건]
  원화 예산: {self._config.budget:,}원
  추정 USD 예산: ${budget_usd:,.2f} (환율 {self._params['fx_rate']})
  최대 포지션: {self._params['max_positions']}개 (현재 {len(self._positions)}개)
  포지션당 최대 비중: {self._params['max_position_pct'] * 100:.0f}%
  일일 거래: {self._daily_limit._count}/{self._daily_limit.max_trades}
  confidence 임계값: {self._params['confidence_threshold']}
  최소 손익비: {self._params['risk_reward_min']}:1

[실제 KIS 해외 주문가능 후보 (AI 호출 전 게이트 통과)]
{actual_buyable_lines}
"""

    @staticmethod
    def _calc_metrics(price: dict, daily: list[dict]) -> dict:
        closes = [d.get("close", 0) for d in daily if d.get("close", 0) > 0]
        vols = [d.get("volume", 0) for d in daily if d.get("volume", 0) > 0]
        current = float(price.get("price", 0))

        def sma(n: int) -> float:
            vals = closes[:n]
            return sum(vals) / len(vals) if vals else 0.0

        def mom(n: int) -> float:
            if current <= 0 or len(closes) <= n or closes[n] <= 0:
                return 0.0
            return (current - closes[n]) / closes[n] * 100

        avg_vol = int(sum(vols[:20]) / min(len(vols), 20)) if vols else 0
        return {
            "sma_5": sma(5),
            "sma_20": sma(20),
            "mom_5d": mom(5),
            "mom_20d": mom(20),
            "avg_vol_20": avg_vol,
            "above_sma20": current > sma(20) if sma(20) else False,
        }

    def _format_position_line(self, pos: dict) -> str:
        current = pos.get("entry_price", 0.0)
        try:
            info = self._get_market().get_us_price(pos["symbol"], pos.get("exchange", "NAS"))
            if info and info.get("price"):
                current = float(info["price"])
        except Exception:
            pass
        entry = float(pos.get("entry_price", 0))
        pnl = (current - entry) * int(pos.get("qty", 0))
        pct = ((current - entry) / entry * 100) if entry > 0 else 0
        return (
            f"  {pos['symbol']} {pos.get('qty', 0)}주 "
            f"@${entry:.2f} → ${current:.2f} ({pct:+.2f}%, ${pnl:+.2f})"
        )

    def _budget_usd(self) -> float:
        fx = float(self._params.get("fx_rate", 1400.0) or 1400.0)
        return (self._config.budget / fx) if self._config.budget > 0 else 0.0

    def _build_prompt(self, context: str) -> str:
        return f"""당신은 월스트리트 최고 레벨의 적극형 미국주식 트레이더입니다.
목표는 완벽한 확실성이 아니라 반복 가능한 양수 기대값입니다. 단, 주문 실수와 과도한 손실은 규칙 가드가 최종 차단합니다.

{context}

## 판단 지침
- 워치리스트 안의 고유동성 미국주식/ETF만 선택하세요.
- BUY는 반드시 예산상 1주 이상 가능한 종목만 선택하세요. 실제 KIS 주문가능수량은 주문 직전 코드가 별도로 확인합니다.
- 강한 추세, 거래량 확장, 섹터/뉴스 모멘텀, 지수 리스크를 함께 비교하세요.
- 레버리지 ETF(SOXL/TQQQ)는 변동성을 감안해 confidence와 손익비를 더 엄격히 보세요.
- BUY는 지정가 진입 기준 손익비 1.5:1 이상, 독립 근거 2개 이상이면 적극 검토합니다.
- 이미 보유 중인 종목의 BUY는 추매입니다. 보유 중이라는 이유만으로 BUY를 금지하지 마세요.
- SELL은 손절, 목표 도달, 모멘텀 붕괴, 더 좋은 기회로 교체할 때 선택합니다.
- SELL은 전량 청산뿐 아니라 분할 매도도 가능합니다. 일부만 줄일 때는 sell_qty 또는 position_pct를 명시하세요.
- HOLD는 데이터 부족, 장외, 손익비 부족, 모든 후보가 음수 기대값일 때만 선택합니다.

반드시 stdout에 JSON만 출력하세요:
```json
{{
  "decision": "BUY | SELL | HOLD",
  "confidence": 0.0,
  "symbol": "NVDA",
  "exchange": "NAS",
  "name": "NVIDIA",
  "order_type": "LIMIT",
  "limit_price": 0.0,
  "target_price": 0.0,
  "stop_loss": 0.0,
  "sell_qty": 0,
  "position_pct": 0.0,
  "bull_summary": "강세 근거",
  "bear_summary": "약세/리스크",
  "trader_reason": "최종 판단"
}}
```"""

    def _invoke_ai(self, prompt: str) -> str:
        from src.bots.ai_caller import invoke_ai
        return invoke_ai(
            prompt,
            self._params,
            timeout=int(self._params.get("codex_timeout", 180)),
            bot_id=self.BOT_ID,
        ) or ""

    @staticmethod
    def _parse_decision(output: str) -> dict | None:
        if not output:
            return None
        decoder = json.JSONDecoder()
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", output, re.DOTALL)
        candidates = [fence.group(1)] if fence else []
        candidates.append(output)
        for raw in candidates:
            i = 0
            while i < len(raw):
                j = raw.find("{", i)
                if j < 0:
                    break
                try:
                    obj, end = decoder.raw_decode(raw, j)
                    if isinstance(obj, dict) and "decision" in obj:
                        obj["decision"] = str(obj.get("decision", "")).upper().strip()
                        obj["confidence"] = max(0.0, min(1.0, float(obj.get("confidence", 0))))
                        if obj["decision"] in ("BUY", "SELL", "HOLD"):
                            return obj
                    i = end
                except (json.JSONDecodeError, ValueError, TypeError):
                    i = j + 1
        return None

    def _check_positions(self) -> list[dict]:
        actions = []
        for pos in list(self._positions):
            symbol = pos["symbol"]
            try:
                info = self._get_market().get_us_price(symbol, pos.get("exchange", "NAS"))
                current = float(info.get("price", 0)) if info else 0.0
            except Exception:
                current = 0.0
            if current <= 0:
                continue

            pos["peak_price"] = max(float(pos.get("peak_price", pos["entry_price"])), current)
            entry = float(pos["entry_price"])
            pct_from_entry = (current - entry) / entry * 100 if entry > 0 else 0.0
            pct_from_peak = (current - float(pos["peak_price"])) / float(pos["peak_price"]) * 100

            if pct_from_entry <= float(self._params["hard_stop_pct"]):
                actions.append({"symbol": symbol, "reason": f"하드손절 {pct_from_entry:.2f}%", "price": current})
            elif pct_from_entry >= float(self._params["take_profit_pct"]):
                actions.append({"symbol": symbol, "reason": f"익절 {pct_from_entry:.2f}%", "price": current})
            elif pct_from_peak <= float(self._params["trailing_stop_pct"]):
                actions.append({"symbol": symbol, "reason": f"트레일링 {pct_from_peak:.2f}%", "price": current})
            elif self._held_days(pos) >= int(self._params["max_hold_days"]):
                actions.append({"symbol": symbol, "reason": f"최대 보유일 초과 {self._held_days(pos)}일", "price": current})

        return actions

    @staticmethod
    def _held_days(pos: dict) -> int:
        try:
            entry = datetime.fromisoformat(pos.get("bought_at", ""))
            return max(0, (kst_now() - entry).days)
        except Exception:
            return 0

    def _should_skip_ai(self) -> str:
        session = self._us_session()
        import src.trading_mode as tm
        if not session["can_buy"] and not session["can_sell"]:
            return f"매매 불가 세션: {session['label']}"
        if tm.current_mode() == tm.DISABLED and not self._positions:
            return "주문 비활성화 상태 + 보유 없음"
        if not self._daily_limit.can_trade() and not self._positions:
            return "일일 거래 제한 소진 + 보유 없음"
        if session["minutes_to_close"] < 5 and not self._positions:
            return "마감 5분 이내 + 보유 없음"
        if not self._positions and session["can_buy"] and self._daily_limit.can_trade():
            preflight = self._us_buy_preflight()
            self._last_preflight = preflight
            if not preflight.get("buyable_candidates"):
                return preflight.get("reason", "실제 해외 매수 가능 종목 없음")
        return ""

    def _us_buy_preflight(self) -> dict:
        """AI 호출 전 미국주식 실제 주문가능 후보를 확인한다."""
        import src.trading_mode as tm

        mode = tm.current_mode()
        if mode == tm.DISABLED:
            return {"buyable_candidates": [], "reason": "주문 비활성화 상태"}

        market = self._get_market()
        budget_usd = self._budget_usd()
        if budget_usd <= 0:
            return {"buyable_candidates": [], "reason": "추정 USD 예산 0"}

        priced = []
        for item in self._watchlist:
            try:
                info = market.get_us_price(item["symbol"], item["exchange"])
                price = float((info or {}).get("price", 0) or 0)
                if price <= 0 or price > budget_usd:
                    continue
                priced.append({**item, "price": price})
            except Exception:
                continue

        if not priced:
            return {
                "buyable_candidates": [],
                "reason": f"예산 내 미국주식 후보 없음: 추정 USD ${budget_usd:,.2f}",
            }

        candidates = []
        for item in sorted(priced, key=lambda x: x["price"])[:5]:
            try:
                buyable = market.get_us_buyable_qty(item["symbol"], item["exchange"], item["price"])
                qty = int(buyable.get("ord_psbl_qty") or buyable.get("max_ord_psbl_qty") or 0) if buyable else 0
                budget_qty = math.floor(budget_usd / item["price"])
                qty = min(qty, budget_qty)
                if qty > 0:
                    candidates.append({
                        **item,
                        "qty": qty,
                        "amount": float((buyable or {}).get("ovrs_ord_psbl_amt", 0) or 0),
                        "cash": float((buyable or {}).get("ord_psbl_frcr_amt", 0) or 0),
                    })
            except Exception:
                continue

        if candidates:
            return {"buyable_candidates": candidates, "reason": "OK"}

        return {
            "buyable_candidates": [],
            "reason": f"KIS 해외 주문가능수량 0: 예산 내 후보 {len(priced)}개, 추정 USD ${budget_usd:,.2f}",
        }

    def _validate_decision(self, decision: dict) -> str:
        action = decision.get("decision", "")
        if action == "HOLD":
            return ""
        symbol = str(decision.get("symbol", decision.get("code", ""))).upper()
        decision["symbol"] = symbol
        if symbol not in self._watch_map:
            return f"워치리스트 외 종목: {symbol}"
        if decision.get("confidence", 0) < float(self._params["confidence_threshold"]):
            decision["decision"] = "HOLD"
            return ""
        if action == "BUY":
            try:
                current = float(self._get_market().get_us_price(symbol, self._watch_map[symbol]["exchange"]).get("price", 0))
                planned_entry = float(decision.get("limit_price") or 0)
                target = float(decision.get("target_price") or 0)
                stop = float(decision.get("stop_loss") or 0)
            except Exception:
                return f"시세 조회 실패: {symbol}"
            if current <= 0:
                return f"시세 조회 실패: {symbol}"
            if self._budget_usd() > 0 and current > self._budget_usd():
                return f"예산 부족: {symbol} 현재가 ${current:.2f} > 가용 예산 ${self._budget_usd():.2f}"
            if planned_entry <= 0:
                slip = float(self._params["limit_slippage_pct"]) / 100
                planned_entry = round(current * (1 + slip), 2)
                decision["limit_price"] = planned_entry
            if target <= planned_entry:
                decision["target_price"] = round(planned_entry * (1 + float(self._params["take_profit_pct"]) / 100), 2)
                target = float(decision["target_price"])
            if stop <= 0 or stop >= planned_entry:
                decision["stop_loss"] = round(planned_entry * (1 + float(self._params["hard_stop_pct"]) / 100), 2)
                stop = float(decision["stop_loss"])
            rr = (target - planned_entry) / max(planned_entry - stop, 0.01)
            decision["risk_reward"] = round(rr, 3)
            if rr + 0.005 < float(self._params["risk_reward_min"]):
                return f"손익비 부족: {rr:.2f}:1 (진입가 ${planned_entry:.2f} 기준)"
        return ""

    def _execute_decision(self, decision: dict) -> dict:
        block = self._validate_decision(decision)
        if block:
            return {"success": False, "reason": block}

        action = decision.get("decision")
        if action == "HOLD":
            return {"success": True, "action": "hold", "reason": decision.get("trader_reason", "관망")}
        if action == "BUY":
            return self._buy_us(decision)
        if action == "SELL":
            return self._sell_us(decision.get("symbol", ""), decision.get("trader_reason", "AI SELL"), decision)
        return {"success": False, "reason": f"알 수 없는 decision: {action}"}

    def _resolve_sell_qty(self, decision: dict | None, pos: dict) -> int:
        """US SELL 수량 해석. 수량/비중이 없으면 전량 매도한다."""
        held_qty = int(pos.get("qty", 0) or 0)
        if held_qty <= 0:
            return 0

        decision = decision or {}
        raw_qty = decision.get("sell_qty", decision.get("qty", decision.get("quantity")))
        try:
            sell_qty = int(raw_qty) if raw_qty not in (None, "") else 0
        except (TypeError, ValueError):
            sell_qty = 0

        if sell_qty <= 0:
            raw_pct = decision.get("sell_pct", decision.get("position_pct", decision.get("portion_pct")))
            try:
                pct = float(raw_pct) if raw_pct not in (None, "") else 0.0
            except (TypeError, ValueError):
                pct = 0.0
            if pct > 1:
                pct /= 100
            if pct > 0:
                sell_qty = max(1, round(held_qty * pct))

        if sell_qty <= 0:
            sell_qty = held_qty
        return max(1, min(held_qty, sell_qty))

    def _buy_us(self, decision: dict) -> dict:
        session = self._us_session()
        if not session["can_buy"]:
            return {"success": False, "reason": f"신규 매수 불가 세션: {session['label']}"}
        symbol = decision["symbol"]
        meta = self._watch_map[symbol]
        pos_idx = next((i for i, p in enumerate(self._positions) if p["symbol"] == symbol), None)

        if pos_idx is None and len(self._positions) >= int(self._params["max_positions"]):
            return {"success": False, "reason": "최대 포지션 수 도달"}
        if not self._daily_limit.can_trade():
            return {"success": False, "reason": "일일 거래 제한 도달"}

        info = self._get_market().get_us_price(symbol, meta["exchange"])
        current = float(info.get("price", 0)) if info else 0.0
        if current <= 0:
            return {"success": False, "reason": f"시세 조회 실패: {symbol}"}

        position_pct = min(
            float(decision.get("position_pct") or self._params["max_position_pct"]),
            float(self._params["max_position_pct"]),
        )
        budget_usd = self._budget_usd()
        alloc = budget_usd * position_pct if budget_usd > 0 else current
        qty = math.floor(alloc / current)
        if qty <= 0 and self._params.get("allow_min_one_share", True) and budget_usd >= current:
            qty = 1
            alloc = current
        if qty <= 0:
            return {
                "success": False,
                "reason": f"매수 가능 수량 없음: {symbol} 현재가 ${current:.2f}, 배정 ${alloc:.2f}, 총예산 ${budget_usd:.2f}",
            }
        if alloc < float(self._params["min_order_usd"]) and current > budget_usd:
            return {
                "success": False,
                "reason": f"최소 주문/예산 미달: {symbol} 현재가 ${current:.2f}, 총예산 ${budget_usd:.2f}",
            }

        limit_price = float(decision.get("limit_price") or 0)
        if limit_price <= 0:
            slip = float(self._params["limit_slippage_pct"]) / 100
            limit_price = round(current * (1 + slip), 2)

        order_type = str(decision.get("order_type") or self._params["order_type"]).upper()

        buyable = self._get_market().get_us_buyable_qty(symbol, meta["exchange"], limit_price)
        if not buyable:
            return {
                "success": False,
                "reason": f"해외주식 주문가능수량 조회 실패: {symbol} @{limit_price:.2f}",
            }

        kis_qty = int(buyable.get("ord_psbl_qty") or buyable.get("max_ord_psbl_qty") or 0)
        if kis_qty <= 0:
            return {
                "success": False,
                "reason": (
                    f"해외주식 주문가능수량 0: {symbol} @{limit_price:.2f}, "
                    f"주문가능금액 ${buyable.get('ovrs_ord_psbl_amt', 0):,.2f}, "
                    f"외화주문가능 ${buyable.get('ord_psbl_frcr_amt', 0):,.2f}. "
                    "계좌 해외주식 거래신청/달러 예수금/통합증거금 상태 확인 필요"
                ),
            }
        if qty > kis_qty:
            qty = kis_qty

        est_krw = int(limit_price * qty * float(self._params["fx_rate"]))
        import src.trading_mode as tm
        ok, msg = tm.validate_order(symbol, est_krw, self._get_pipe_id())
        if not ok:
            return {"success": False, "reason": msg}

        result = self._get_order().buy_us(
            symbol=symbol,
            quantity=qty,
            price=limit_price,
            exchange=meta["exchange"],
            order_type=order_type,
        )
        if not result.success:
            return {"success": False, "reason": result.message, "order_no": result.order_no}

        if pos_idx is not None:
            pos = self._positions[pos_idx]
            old_qty = int(pos.get("qty", 0) or 0)
            old_entry = float(pos.get("entry_price", 0) or 0)
            total_qty = old_qty + qty
            avg_entry = ((old_qty * old_entry) + (qty * limit_price)) / total_qty if total_qty > 0 else limit_price
            pos["qty"] = total_qty
            pos["entry_price"] = round(avg_entry, 4)
            pos["peak_price"] = max(float(pos.get("peak_price", old_entry) or 0), limit_price)
            pos["target_price"] = float(decision.get("target_price") or pos.get("target_price", 0) or 0)
            pos["stop_loss"] = float(decision.get("stop_loss") or pos.get("stop_loss", 0) or 0)
            pos["last_added_at"] = kst_now().isoformat()
            pos["adds"] = int(pos.get("adds", 0) or 0) + 1
            pos["order_no"] = result.order_no
        else:
            pos = {
                "symbol": symbol,
                "exchange": meta["exchange"],
                "name": decision.get("name") or meta["name"],
                "qty": qty,
                "entry_price": limit_price,
                "peak_price": limit_price,
                "target_price": float(decision.get("target_price") or 0),
                "stop_loss": float(decision.get("stop_loss") or 0),
                "bought_at": kst_now().isoformat(),
                "order_no": result.order_no,
                "adds": 0,
            }
            self._positions.append(pos)
        self._daily_limit.record_trade()
        self._log_us_trade("buy", symbol, qty, limit_price, result.order_no, reason=decision.get("trader_reason", ""))
        action_label = "US 추매" if pos_idx is not None else "US 매수"
        self._post_to_board("trade", f"{action_label} {symbol} {qty}주 @${limit_price:.2f}", symbol, decision.get("trader_reason", ""))
        self._save_state()
        return {
            "success": True,
            "action": "add_buy" if pos_idx is not None else "buy",
            "order_no": result.order_no,
            "symbol": symbol,
            "qty": qty,
            "price": limit_price,
            "total_qty": pos.get("qty", qty),
            "avg_price": pos.get("entry_price", limit_price),
        }

    def _sell_us(self, symbol: str, reason: str = "", decision: dict | None = None) -> dict:
        symbol = str(symbol or "").upper()
        idx = next((i for i, p in enumerate(self._positions) if p["symbol"] == symbol), None)
        if idx is None:
            return {"success": False, "reason": f"보유하지 않은 종목: {symbol}"}
        session = self._us_session()
        if not session["can_sell"]:
            return {"success": False, "reason": f"매도 불가 세션: {session['label']}"}

        pos = self._positions[idx]
        info = self._get_market().get_us_price(symbol, pos.get("exchange", "NAS"))
        current = float(info.get("price", 0)) if info else 0.0
        if current <= 0:
            return {"success": False, "reason": f"시세 조회 실패: {symbol}"}

        slip = float(self._params["limit_slippage_pct"]) / 100
        limit_price = round(current * (1 - slip), 2)
        sell_qty = self._resolve_sell_qty(decision, pos)
        original_qty = int(pos.get("qty", 0) or 0)
        if sell_qty <= 0:
            return {"success": False, "reason": f"매도 가능 수량 없음: {symbol}"}

        result = self._get_order().sell_us(
            symbol=symbol,
            quantity=sell_qty,
            price=limit_price,
            exchange=pos.get("exchange", "NAS"),
            order_type=str(self._params["order_type"]).upper(),
        )
        if not result.success:
            return {"success": False, "reason": result.message, "order_no": result.order_no}

        entry = float(pos.get("entry_price", 0))
        pnl_usd = (limit_price - entry) * sell_qty if entry > 0 else 0.0
        pnl_krw = int(pnl_usd * float(self._params["fx_rate"]))
        self._realized_pnl += pnl_krw
        if sell_qty >= original_qty:
            self._positions.pop(idx)
            action = "sell"
        else:
            pos["qty"] = original_qty - sell_qty
            pos["last_sold_at"] = kst_now().isoformat()
            pos["sells"] = int(pos.get("sells", 0) or 0) + 1
            action = "partial_sell"
        self._daily_limit.record_trade()
        self._log_us_trade("sell", symbol, sell_qty, limit_price, result.order_no, reason=reason, realized_pnl=pnl_krw)
        label = "US 분할 매도" if action == "partial_sell" else "US 매도"
        self._post_to_board("trade", f"{label} {symbol} {sell_qty}주 @${limit_price:.2f} ({pnl_usd:+.2f} USD)", symbol, reason)
        self._save_state()
        return {
            "success": True,
            "action": action,
            "order_no": result.order_no,
            "symbol": symbol,
            "qty": sell_qty,
            "price": limit_price,
            "realized_pnl": pnl_krw,
            "remaining_qty": pos.get("qty", 0) if action == "partial_sell" else 0,
        }

    def _log_us_trade(self, side: str, symbol: str, qty: int, price_usd: float,
                      order_no: str = "", reason: str = "", realized_pnl: int = 0):
        try:
            from src.db import get_db
            price_krw = int(price_usd * float(self._params["fx_rate"]))
            get_db().log_trade(
                self._get_pipe_id(),
                symbol,
                symbol,
                side,
                qty,
                price_krw,
                order_no=order_no,
                success=True,
                reason=f"US ${price_usd:.2f} | {reason}",
                source=self.BOT_ID,
                realized_pnl=realized_pnl,
            )
        except Exception as e:
            logger.warning("[%s] US 거래 DB 기록 실패: %s", self.BOT_ID, e)

    def scan(self) -> list[dict]:
        return []

    def decide(self, candidates: list[dict]) -> dict:
        return {"action": "skip", "reason": "AI 분석 대기"}

    def execute(self, decision: dict) -> dict:
        return {"success": True, "action": "skip"}

    async def _loop(self):
        logger.info("[%s] 시작 — 미국주식 AI, interval=%ds", self.BOT_ID, self._config.interval_seconds)
        while self._running:
            try:
                self._scan_count += 1
                now = kst_now()
                loop = asyncio.get_running_loop()
                self._reload_params_if_needed()

                for exit_action in self._check_positions():
                    result = self._sell_us(exit_action["symbol"], exit_action["reason"])
                    if result.get("success"):
                        self._publish_event("bot_trade", {"action": "sell", "code": exit_action["symbol"], "result": result})

                output = ""
                ai_decision = None
                skip_reason = self._should_skip_ai()
                if not skip_reason:
                    context = await loop.run_in_executor(None, self._collect_data)
                    prompt = self._build_prompt(context)
                    output = await loop.run_in_executor(None, self._invoke_ai, prompt)
                    ai_decision = self._parse_decision(output)
                else:
                    logger.info("[%s] AI 호출 생략: %s", self.BOT_ID, skip_reason)

                self._last_analysis = {
                    "time": now.isoformat(),
                    "scan": self._scan_count,
                    "decision": ai_decision,
                    "execution": {},
                    "raw_output_length": len(output),
                }

                result = {}
                action = "hold"
                detail = skip_reason or "AI 호출 실패 또는 파싱 실패"
                if ai_decision:
                    action = ai_decision["decision"].lower()
                    detail = ai_decision.get("trader_reason", "")
                    self._debate_log.append({
                        "time": now.strftime("%H:%M"),
                        "decision": ai_decision["decision"],
                        "confidence": ai_decision.get("confidence", 0),
                        "symbol": ai_decision.get("symbol", ""),
                        "bull": ai_decision.get("bull_summary", ""),
                        "bear": ai_decision.get("bear_summary", ""),
                        "trader": detail,
                    })
                    self._debate_log = self._debate_log[-20:]
                    self._post_to_board("opinion", f"[US {ai_decision['decision']}] {detail}", ai_decision.get("symbol", ""))

                    if ai_decision["decision"] in ("BUY", "SELL"):
                        result = self._execute_decision(ai_decision)
                        if result.get("success"):
                            action = result.get("action") or ai_decision["decision"].lower()
                            self._publish_event("bot_trade", {
                                "action": action,
                                "code": ai_decision.get("symbol", ""),
                                "result": result,
                            })
                        else:
                            action = f"blocked_{ai_decision['decision'].lower()}"
                            detail = f"주문 차단: {result.get('reason', result.get('message', 'unknown'))} | AI: {detail}"
                            self._post_to_board(
                                "alert",
                                f"[US {ai_decision['decision']} 차단] {result.get('reason', '')}",
                                ai_decision.get("symbol", ""),
                                detail,
                            )
                        self._last_analysis["execution"] = result

                self._trade_log.append({
                    "scan": self._scan_count,
                    "time": now.strftime("%H:%M:%S"),
                    "action": action,
                    "detail": detail,
                    "result": {
                        "decision": ai_decision,
                        "execution": result,
                    } if ai_decision else result,
                })
                self._trade_log = self._trade_log[-50:]

                if ai_decision or self._scan_count == 1 or self._scan_count % 10 == 0:
                    write_bot_log(self.BOT_ID, {
                        "scan": self._scan_count,
                        "time": now.isoformat(),
                        "positions": self._positions,
                        "action": action,
                        "detail": detail,
                        "decision": ai_decision,
                        "execution": result,
                        "daily_trades": {"used": self._daily_limit._count, "max": self._daily_limit.max_trades},
                    })

                self._save_state()
                self._log_decision({
                    "action": action,
                    "code": ai_decision.get("symbol", "") if ai_decision else "",
                    "reason": detail,
                    "confidence": ai_decision.get("confidence", 0) if ai_decision else 0,
                })
            except Exception as e:
                logger.error("[%s] 스캔 오류: %s", self.BOT_ID, e, exc_info=True)

            session = self._us_session()
            sleep_s = self._config.interval_seconds if session["can_buy"] or session["can_sell"] else 600
            await asyncio.sleep(sleep_s)

    def status(self) -> dict:
        total_unrealized_usd = 0.0
        positions = []
        for pos in self._positions:
            current = float(pos.get("entry_price", 0))
            try:
                info = self._get_market().get_us_price(pos["symbol"], pos.get("exchange", "NAS"))
                if info and info.get("price"):
                    current = float(info["price"])
            except Exception:
                pass
            entry = float(pos.get("entry_price", 0))
            qty = int(pos.get("qty", 0))
            pnl_usd = (current - entry) * qty if entry > 0 else 0.0
            total_unrealized_usd += pnl_usd
            positions.append({
                "code": pos["symbol"],
                "symbol": pos["symbol"],
                "name": pos.get("name", ""),
                "qty": qty,
                "entry_price": entry,
                "current_price": current,
                "unrealized_usd": round(pnl_usd, 2),
                "unrealized": int(pnl_usd * float(self._params["fx_rate"])),
                "bought_at": pos.get("bought_at", ""),
            })

        unrealized_krw = int(total_unrealized_usd * float(self._params["fx_rate"]))
        session = self._us_session()
        return {
            "running": self._running,
            "bot_id": self.BOT_ID,
            "scan_count": self._scan_count,
            "holding": {
                "code": positions[0]["symbol"] if positions else "",
                "qty": positions[0]["qty"] if positions else 0,
                "price": positions[0]["entry_price"] if positions else 0,
            },
            "positions": positions,
            "positions_count": len(self._positions),
            "max_positions": self._params["max_positions"],
            "pnl": {
                "realized": self._realized_pnl,
                "unrealized": unrealized_krw,
                "total": self._realized_pnl + unrealized_krw,
                "unrealized_usd": round(total_unrealized_usd, 2),
            },
            "config": {
                "interval": self._config.interval_seconds,
                "budget": self._config.budget,
                "confidence_threshold": self._params["confidence_threshold"],
                "max_daily_trades": self._daily_limit.max_trades,
            },
            "us_session": {
                "label": session["label"],
                "can_buy": session["can_buy"],
                "can_sell": session["can_sell"],
                "minutes_to_close": session["minutes_to_close"],
            },
            "last_analysis": self._last_analysis,
            "debate_log": self._debate_log[-5:],
            "recent_trades": self._trade_log[-5:],
            "daily_trades": {"used": self._daily_limit._count, "max": self._daily_limit.max_trades},
        }

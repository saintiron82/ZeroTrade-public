"""TradeEngine — centralized trading engine for all bots.

Consolidates order execution, PnL calculation, DB logging,
pipe synchronization, and board posting into a single class.
All bots should use TradeEngine for buy/sell operations.
"""

import logging

from src.kis.market import KISMarket
from src.kis.order import KISOrder, OrderType

logger = logging.getLogger(__name__)

# ETF 섹터 분류
ETF_SECTORS = {
    "091160": "반도체", "091170": "반도체", "091180": "반도체",
    "305720": "2차전지",
    "069500": "시장지수", "229200": "시장지수", "114800": "시장지수", "122630": "시장지수",
    "130680": "에너지", "139260": "우주항공",
    "005930": "반도체", "000660": "반도체",
    "102780": "대형주", "102110": "대형주",
    "117700": "산업재", "117680": "화학",
    "132030": "골드", "117460": "헬스케어",
}


def stock_name_for(code: str) -> str:
    try:
        from src.services.etf_signal import ETF_MAPPING
        name = ETF_MAPPING.get(str(code), {}).get("name", "")
        if name:
            return name
        # ETF_MAPPING에 없는 코드(예: TIGER 인버스 123310)는 마스터 파일에서 조회
        from src.kis.master import stock_name_by_code
        return stock_name_by_code(code)
    except Exception:
        return ""


class TradeEngine:
    """Centralized trading engine.

    Args:
        bot_id: Bot identifier (e.g. "stealth-volume").
        instance_id: Pipe-scoped instance key (e.g. "strat-001:stealth-volume").
        budget: 봇이 자율 집행할 전액 (= 파이프 예산 × 봇 ratio).
            봇은 이 금액 안에서 언제/얼마나 살지 전적으로 자율 판단한다.
            봇 내부에 추가 퍼센트 캡을 두는 이중 제한은 금지 — 예산 분배는
            파이프의 ratio 기능이 유일한 정의 경로다.
            0이면 무제한 (테스트/특수 상황용).
    """

    def __init__(self, bot_id: str, instance_id: str = "", budget: int = 0,
                 guard_config: dict | None = None):
        self.bot_id = bot_id
        self.instance_id = instance_id
        self.budget = budget
        self.cash: int = budget  # 봇 가상 계좌 현금. refresh_cash()로 trades에서 재계산.
        self.realized_pnl: int = 0
        # 매수 전면 차단 사유 — pipe_runner가 ratio 0% 봇(커스토디언 모드)에 설정.
        self.buy_block_reason: str = ""

        self._market: KISMarket | None = None
        self._order: KISOrder | None = None
        self._strategy_mgr = None

        # Guard config (all disabled by default)
        gc = guard_config or {}
        self._guard = {
            "daily_loss_limit_pct": gc.get("daily_loss_limit_pct", 0),
            "market_drop_block_pct": gc.get("market_drop_block_pct", 0),
            "trend_filter_enabled": gc.get("trend_filter_enabled", False),
            "duplicate_check_enabled": gc.get("duplicate_check_enabled", False),
            "max_sector_exposure": gc.get("max_sector_exposure", 0),
        }
        self._daily_loss: int = 0
        self._daily_loss_date: str = ""
        self._cache: dict = {}

    # ── Properties ──

    @property
    def pipe_id(self) -> str:
        """Extract pipe ID from instance_id."""
        return self.instance_id.split(":")[0] if ":" in self.instance_id else ""

    # ── Lazy init ──

    def _get_market(self) -> KISMarket:
        if self._market is None:
            self._market = KISMarket()
        return self._market

    def _get_order(self) -> KISOrder:
        if self._order is None:
            self._order = KISOrder()
        return self._order

    def _kis_balance_qty(self, code: str) -> int:
        """KIS 계좌의 실제 보유 수량 조회 (파이프 전체 — 다른 봇 보유 포함).

        Returns:
            실 보유 수량 (>= 0). 조회 실패 시 -1 (클램프 우회 신호).
        """
        try:
            balance = self._get_market().get_balance()
            holdings = balance.get("holdings", []) if balance else []
            for h in holdings:
                if h.get("stock_code") == code:
                    return int(h.get("quantity", 0) or 0)
            return 0
        except Exception as e:
            logger.warning("[%s] KIS 잔고 조회 실패 (클램프 생략): %s", self.bot_id, e)
            return -1

    def _own_ledger_qty(self, code: str) -> int:
        """봇 자기 ledger 기준 보유 수량 (source=bot_id trades에서 결정론적 계산).

        봇 독립 원칙: 봇은 자기가 매수한 분량만 매도 가능. KIS에 다른 봇 주식이
        쌓여 있어도 그 분량은 봇 관리 대상 아님.

        Returns:
            자기 ledger 보유 수량 (>= 0). pipe_id/bot_id 없으면 -1 (클램프 우회).
        """
        if not self.pipe_id or not self.bot_id:
            return -1
        try:
            from src.db import get_db
            from src.bots.ledger import BotLedger
            trades = get_db().get_trades(
                pipe_id=self.pipe_id, source=self.bot_id, limit=10000)
            ledger = BotLedger.from_trades(budget=0, trades=trades)
            for p in ledger.positions:
                if p["code"] == code:
                    return int(p["qty"])
            return 0
        except Exception as e:
            logger.warning("[%s] own ledger 조회 실패: %s", self.bot_id, e)
            return -1

    # ── Public API ──

    def buy(self, code: str, qty: int, price: int = 0) -> dict:
        """Execute buy order with logging, sync, and board posting.

        Returns:
            {"success": bool, "order_no": str, "message": str}
        """
        # Pre-flight guards
        block = self._pre_buy_checks(code, price)
        if block:
            logger.info("[%s] 매수 차단: %s (%s)", self.bot_id, code, block)
            return {"success": False, "order_no": "", "message": block}

        # Cash check (봇 가상 계좌)
        # budget이 있는 봇은 cash가 0 이하(과거 과집행)여도 반드시 차단한다 —
        # 기존 `self.cash > 0 and` 조건은 마이너스 cash를 무제한으로 풀어주는 결함이었음 (2026-06-12).
        cost = qty * max(price, 1)
        if self.budget > 0 and cost > self.cash:
            return {"success": False, "order_no": "", "message": f"가용 현금 부족 ({self.cash:,}원 < {cost:,}원)"}

        import src.trading_mode as tm
        ok, msg = tm.validate_order(code, qty * max(price, 1), self.pipe_id)
        if not ok:
            return {"success": False, "order_no": "", "message": msg}

        order_type = OrderType.LIMIT if price > 0 else OrderType.MARKET
        order = self._get_order()
        result = order.buy(stock_code=code, quantity=qty, price=price, order_type=order_type)

        if result.success:
            trade_id = self._log_trade("buy", code, qty, price, result.order_no)
            self._sync_pipe_buy(code, qty, price)
            self.cash -= qty * max(price, 1)
            self._post_to_board(f"매수 {code} {qty}주 @{price:,}원", code)
            return {
                "success": result.success,
                "order_no": result.order_no,
                "message": result.message,
                "trade_id": trade_id,
            }

        return {
            "success": result.success,
            "order_no": result.order_no,
            "message": result.message,
        }

    def sell(self, code: str, qty: int, entry_price: int = 0,
             order_type: OrderType = OrderType.MARKET,
             reason: str = "", basis: dict | None = None) -> dict:
        """Execute sell order with PnL calculation, logging, sync, and board posting.

        Args:
            code: Stock code.
            qty: Quantity to sell.
            entry_price: Average buy price for PnL calculation. 0 skips PnL.
            order_type: Order type (MARKET, LIMIT, AFTER_HOURS_CLOSE, etc.).
            reason: Human-readable sell trigger to persist with the trade.
            basis: Prices and thresholds the bot knew when it decided to sell.

        Returns:
            {"success": bool, "order_no": str, "message": str,
             "sell_price": int, "realized_pnl": int, "sell_basis": dict}
        """
        if not code or qty <= 0:
            return {
                "success": False, "order_no": "", "message": "매도할 종목 없음",
                "sell_price": 0, "realized_pnl": 0,
            }

        # KIS 실잔고 클램프 — phantom 포지션 감지(KIS=0)와 외부 매도 반영
        actual_qty = self._kis_balance_qty(code)
        if actual_qty == 0:
            msg = f"실잔고 부족 (계좌 보유 0주, 요청 {qty}주)"
            logger.warning("[%s] SELL 차단: %s %s", self.bot_id, code, msg)
            return {
                "success": False, "order_no": "", "message": msg,
                "sell_price": 0, "realized_pnl": 0,
            }
        clamped_qty = qty if actual_qty < 0 else min(qty, actual_qty)
        if clamped_qty < qty:
            logger.warning(
                "[%s] SELL KIS 클램프: %s 요청 %d → 실잔고 기준 %d",
                self.bot_id, code, qty, clamped_qty,
            )

        # 자기 ledger 클램프 (봇 독립 원칙) — 봇은 자기가 산 분량만 매도 가능.
        # 같은 종목을 다른 봇이 들고 있어도 그 분량은 관리 대상 아님.
        own_qty = self._own_ledger_qty(code)
        if own_qty == 0:
            msg = f"자기 ledger 보유 0주 (요청 {qty}주, 다른 봇 보유분은 매도 대상 아님)"
            logger.warning("[%s] SELL 차단: %s %s", self.bot_id, code, msg)
            return {
                "success": False, "order_no": "", "message": msg,
                "sell_price": 0, "realized_pnl": 0,
            }
        if own_qty > 0 and clamped_qty > own_qty:
            logger.warning(
                "[%s] SELL 자기 ledger 클램프: %s KIS %d → ledger 기준 %d",
                self.bot_id, code, clamped_qty, own_qty,
            )
            clamped_qty = own_qty

        order = self._get_order()
        result = order.sell(stock_code=code, quantity=clamped_qty, order_type=order_type)

        if not result.success:
            return {
                "success": False,
                "order_no": result.order_no,
                "message": result.message,
                "sell_price": 0,
                "realized_pnl": 0,
            }

        # Price fallback: result.price → market price → entry_price
        sell_price = result.price
        if not sell_price:
            try:
                info = self._get_market().get_price(code)
                sell_price = info.get("price", 0) if info else 0
            except Exception:
                pass
        sell_price = sell_price or entry_price

        # PnL
        realized = (sell_price - entry_price) * clamped_qty if entry_price > 0 else 0
        self.realized_pnl += realized
        self._track_daily_loss(realized)

        sell_basis = {
            **(basis or {}),
            "entry_price": entry_price,
            "sell_price": sell_price,
            "quantity": clamped_qty,
            "realized_pnl": realized,
        }
        trade_reason = self._format_trade_reason("sell", reason, sell_basis, realized)

        # Side effects
        trade_id = self._log_trade(
            "sell", code, clamped_qty, sell_price, result.order_no, realized, trade_reason
        )
        self._sync_pipe_sell(code, clamped_qty, sell_price)
        self.cash += clamped_qty * sell_price
        board_content = f"매도 {code} {clamped_qty}주 @{sell_price:,}원 (손익 {realized:+,}원)"
        if reason:
            board_content += f" {reason}"
        self._post_to_board(
            board_content, code
        )

        return {
            "success": True,
            "order_no": result.order_no,
            "message": result.message,
            "trade_id": trade_id,
            "sell_price": sell_price,
            "realized_pnl": realized,
            "sell_basis": sell_basis,
            "clamped_qty": clamped_qty,
        }

    def get_buyable_qty(self, stock_code: str, price: int) -> int:
        """가용 현금과 KIS 한도 내 최대 매수 수량.

        봇 cash = budget − 누적 매수금 + 누적 매도금 (자기 trades만).
        다른 봇의 거래에 영향받지 않는다.
        """
        market = self._get_market()
        buyable = market.get_buyable_qty(stock_code, price)
        max_qty = buyable.get("max_qty", 0)
        if self.cash > 0 and price > 0:
            cash_qty = self.cash // price
            max_qty = min(max_qty, cash_qty)
        return max_qty

    def refresh_cash(self):
        """trades 이력에서 cash/realized_pnl을 재계산.

        봇 시작 시 또는 외부 변경 후 호출. BotLedger가 단일 진실원천.
        pipe_id가 없으면 budget 그대로 유지.
        """
        if not self.pipe_id:
            return
        try:
            from src.db import get_db
            from src.bots.ledger import BotLedger
            trades = get_db().get_trades(pipe_id=self.pipe_id, source=self.bot_id, limit=1000)
            ledger = BotLedger.from_trades(budget=self.budget, trades=trades)
            self.cash = ledger.cash
            self.realized_pnl = ledger.realized_pnl
        except Exception as e:
            logger.warning("[%s] cash 갱신 실패: %s", self.bot_id, e)

    def can_afford(self, amount: int) -> bool:
        """cash 범위 내 매수 가능 여부."""
        return amount <= self.cash

    def close(self):
        """Cleanup market/order resources."""
        if self._market:
            self._market.close()
            self._market = None
        if self._order:
            self._order.close()
            self._order = None

    # ── Pre-buy guards ──

    def _pre_buy_checks(self, code: str, price: int) -> str:
        """Run all pre-buy guards. Returns block reason or empty string."""
        if self.buy_block_reason:
            return self.buy_block_reason
        if r := self._check_daily_loss_limit():
            return r
        if r := self._check_derivative_etf(code):
            return r
        if r := self._check_market_index(code):
            return r
        if r := self._check_trend_filter(code):
            return r
        if r := self._check_sector_exposure(code):
            return r
        return ""

    def _check_daily_loss_limit(self) -> str:
        limit_pct = self._guard["daily_loss_limit_pct"]
        if not limit_pct or self.budget <= 0:
            return ""
        self._reset_daily_if_new_day()
        threshold = int(self.budget * limit_pct / 100)
        if self._daily_loss <= threshold:
            return f"일일 손실 한도 ({self._daily_loss:,} <= {threshold:,}원)"
        return ""

    def _check_derivative_etf(self, code: str) -> str:
        """파생ETF(인버스/레버리지) 차단 — 전역 상급 옵션 (기본 OFF).

        per-bot guard_config가 아니라 전역 설정을 읽는다.
        계좌의 파생상품ETF 권한 문제이지 봇 리스크 정책이 아니기 때문.
        """
        import src.trading_mode as tm
        if tm.derivative_etf_enabled():
            return ""
        from src.kis.master import is_derivative_etf
        if is_derivative_etf(stock_name_for(code)):
            return "파생ETF 비활성 (상급 옵션 OFF)"
        return ""

    def _check_market_index(self, code: str) -> str:
        block_pct = self._guard["market_drop_block_pct"]
        if not block_pct:
            return ""
            
        name = stock_name_for(code)
        if any(keyword in name for keyword in ("인버스", "골드", "원유", "달러")):
            return ""
            
        rate = self._cached_get("market_index", "069500", ttl=300,
                                fetcher=self._fetch_change_rate)
        if rate is None:
            return ""
        if rate <= block_pct:
            return f"KOSPI 급락 ({rate:+.1f}% <= {block_pct:+.1f}%)"
        return ""

    def _check_trend_filter(self, code: str) -> str:
        if not self._guard["trend_filter_enabled"]:
            return ""
        sma = self._cached_get(f"sma20:{code}", code, ttl=3600,
                               fetcher=self._fetch_sma20)
        if not sma:
            return ""
        try:
            info = self._get_market().get_price(code)
            current = info.get("price", 0) if info else 0
        except Exception:
            return ""
        if current and current < sma:
            return f"하락추세 ({current:,} < SMA20 {sma:,})"
        return ""

    def _check_duplicate(self, code: str) -> str:
        if not self._guard["duplicate_check_enabled"] or not self.pipe_id:
            return ""
        try:
            from src.db import get_db
            posts = get_db().get_board(self.pipe_id, 20)
            for p in posts:
                if (p.get("code") == code and p.get("type") == "trade"
                    and "매수" in p.get("content", "")
                    and p.get("bot_id") != self.bot_id):
                    return f"다른 봇 보유 중 ({p.get('bot_id')})"
        except Exception:
            pass
        return ""

    def _check_sector_exposure(self, code: str) -> str:
        max_exp = self._guard.get("max_sector_exposure", 0)
        if not max_exp or not self.pipe_id:
            return ""
        sector = ETF_SECTORS.get(code, "")
        if not sector:
            return ""
        try:
            from src.db import get_db
            posts = get_db().get_board(self.pipe_id, 30)
            sector_codes = set()
            for p in posts:
                p_code = p.get("code", "")
                if (p.get("type") == "trade" and "매수" in p.get("content", "")
                    and p_code and ETF_SECTORS.get(p_code, "") == sector):
                    sector_codes.add(p_code)
            # 동일 종목 추매는 신규 섹터 노출 증가가 아니다.
            sector_codes.discard(code)
            if len(sector_codes) >= max_exp:
                return f"섹터 집중 ({sector}: {len(sector_codes)}종목 >= {max_exp})"
        except Exception:
            pass
        return ""

    def _track_daily_loss(self, realized: int):
        if realized >= 0:
            return
        self._reset_daily_if_new_day()
        self._daily_loss += realized

    def _reset_daily_if_new_day(self):
        from src.bots.safeguards import kst_now
        today = kst_now().date().isoformat()
        if self._daily_loss_date != today:
            self._daily_loss_date = today
            self._daily_loss = 0

    def _cached_get(self, key: str, code: str, ttl: int, fetcher):
        import time as _time
        now = _time.time()
        if key in self._cache:
            expiry, val = self._cache[key]
            if now < expiry:
                return val
        try:
            val = fetcher(code)
            self._cache[key] = (now + ttl, val)
            return val
        except Exception:
            return None

    def _fetch_change_rate(self, code: str) -> float:
        info = self._get_market().get_price(code)
        return info.get("change_rate", 0) if info else 0.0

    def _fetch_sma20(self, code: str) -> int:
        bars = self._get_market().get_daily_price(code, "D", 20)
        if not bars or len(bars) < 20:
            return 0
        return sum(b.get("close", 0) for b in bars[:20]) // 20

    # ── Private helpers ──

    def _format_trade_reason(self, side: str, reason: str = "",
                             basis: dict | None = None, realized: int = 0) -> str:
        """Build a compact reason string for DB trade history."""
        text = f"{self.bot_id} {'매수' if side == 'buy' else '매도'}"
        if realized:
            text += f" (실현:{realized:+,}원)"
        if reason:
            text += f" | 사유:{reason}"

        basis = basis or {}
        if basis:
            parts = []
            for key, label in [
                ("entry_price", "진입"),
                ("current_price", "판단현재"),
                ("sell_price", "체결"),
                ("target_price", "목표"),
                ("stop_loss", "손절"),
            ]:
                val = basis.get(key)
                if val not in (None, "", 0):
                    try:
                        parts.append(f"{label}:{int(val):,}")
                    except (TypeError, ValueError):
                        parts.append(f"{label}:{val}")
            if parts:
                text += " | 기준:" + ", ".join(parts)
        return text

    def _log_trade(self, side: str, code: str, qty: int, price: int,
                   order_no: str = "", realized: int = 0,
                   reason_override: str = "") -> int:
        """Write to SQLite DB and trades.json."""
        reason = reason_override or self._format_trade_reason(side, realized=realized)
        stock_name = stock_name_for(code)

        # 1) SQLite DB
        trade_id = 0
        try:
            from src.db import get_db
            trade_id = get_db().log_trade(
                self.pipe_id, code, stock_name, side, qty, price,
                order_no=order_no, success=True,
                reason=reason, source=self.bot_id,
                realized_pnl=realized,
            )
        except Exception as e:
            logger.warning("[%s] DB 기록 실패: %s", self.bot_id, e)

        # 2) trades.json
        try:
            from src.kis.order import OrderResult
            from src.trade.logger import TradeLogger
            result = OrderResult(
                success=True, order_no=order_no, message=reason,
                stock_code=code, side=side, quantity=qty, price=price,
            )
            TradeLogger().log_trade(result, reason=reason, strategy_id=self.pipe_id)
        except Exception as e:
            logger.warning("[%s] trades.json 기록 실패: %s", self.bot_id, e)

        return trade_id

    def _get_strategy_mgr(self):
        if self._strategy_mgr is None:
            from src.strategy import StrategyManager
            self._strategy_mgr = StrategyManager()
        return self._strategy_mgr

    def _sync_pipe_buy(self, code: str, qty: int, price: int):
        """Sync pipe cash/holdings after buy."""
        if not self.pipe_id:
            return
        try:
            self._get_strategy_mgr().record_buy(self.pipe_id, code, stock_name_for(code), qty, price)
        except Exception as e:
            logger.warning("[%s] 파이프 매수 동기화 실패: %s", self.bot_id, e)

    def _sync_pipe_sell(self, code: str, qty: int, price: int):
        """Sync pipe cash/holdings after sell."""
        if not self.pipe_id:
            return
        try:
            self._get_strategy_mgr().record_sell(self.pipe_id, code, qty, price)
        except Exception as e:
            logger.warning("[%s] 파이프 매도 동기화 실패: %s", self.bot_id, e)

    def _post_to_board(self, content: str, code: str = ""):
        """Post trade to pipe board."""
        if not self.pipe_id:
            return
        content = str(content or "")
        try:
            from src.db import get_db
            get_db().post_board(self.pipe_id, self.bot_id, "trade", content, code)
        except Exception as e:
            logger.warning("[%s] 게시판 등록 실패: %s", self.bot_id, e)

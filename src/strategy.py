"""다중 전략 관리 — Strategy, Portfolio, StrategyManager."""

import json
import logging
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime
from pathlib import Path

from src.config import settings, PROJECT_ROOT

logger = logging.getLogger(__name__)

STRATEGIES_FILE = PROJECT_ROOT / "data" / "strategies.json"


def stock_name_for(code: str, fallback: str = "") -> str:
    """Known stock/ETF display name fallback used when order APIs omit names."""
    if fallback:
        return fallback
    try:
        from src.services.etf_signal import ETF_MAPPING
        return ETF_MAPPING.get(str(code), {}).get("name", "")
    except Exception:
        return ""


@dataclass
class Holding:
    """파이프 내 개별 보유종목."""
    code: str
    name: str = ""
    qty: int = 0
    avg_price: int = 0


@dataclass
class Strategy:
    id: str
    name: str
    description: str = ""
    budget: int = 0          # 배정 자금 (원)
    used: int = 0            # 현재 투입 금액
    max_positions: int = 5   # 최대 동시 포지션
    active: bool = True
    created_at: str = ""
    conditions_file: str = ""  # 연결된 조건식 파일

    # 봇 연결 (복수 봇 지원)
    bot_id: str = ""         # 단일 봇 (하위 호환)
    bot_params: dict = field(default_factory=dict)
    bots: list = field(default_factory=list)  # [{bot_id, ratio, params}] 복수 봇

    # 독립 자산 추적
    cash: int = 0            # 현재 현금 (초기값 = budget)
    holdings: list = field(default_factory=list)  # list[Holding as dict]
    realized_pnl: int = 0    # 실현 손익
    trades: list = field(default_factory=list)     # 매매 이력

    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    def bot_budget(self, bot_id: str) -> int:
        """특정 봇에 배정된 금액."""
        for b in self.bots:
            if b.get("bot_id") == bot_id:
                return int(self.budget * b.get("ratio", 0))
        # 단일 봇 호환
        if self.bot_id == bot_id:
            return self.budget
        return 0

    def active_bots(self) -> list[dict]:
        """배정된 봇 목록 (금액 포함). enabled=false인 봇은 제외."""
        if self.bots:
            return [{
                "bot_id": b["bot_id"],
                "ratio": b.get("ratio", 0),
                "budget": int(self.budget * b.get("ratio", 0)),
                "params": b.get("params", {}),
                "enabled": b.get("enabled", True),
            } for b in self.bots if b.get("enabled", True) is not False]
        # 단일 봇 호환
        if self.bot_id:
            return [{"bot_id": self.bot_id, "ratio": 1.0, "budget": self.budget, "params": self.bot_params}]
        return []

    def can_order(self, amount: int) -> bool:
        return self.active and amount <= self.cash

    def profit_rate(self, current_prices: dict[str, int] | None = None) -> float:
        """수익률 (%) = (현재 총가치 - 초기예산) / 초기예산 * 100."""
        if self.budget <= 0:
            return 0.0
        tv = self.total_value(current_prices)
        return round((tv - self.budget) / self.budget * 100, 2)

    def total_value(self, current_prices: dict[str, int] | None = None) -> int:
        """현금 + 보유종목 평가액."""
        prices = current_prices or {}
        holdings_value = sum(
            h.get("qty", 0) * prices.get(h.get("code", ""), h.get("avg_price", 0))
            for h in self.holdings
        )
        return self.cash + holdings_value

    def buy_holding(self, code: str, name: str, qty: int, price: int):
        """매수 기록."""
        name = stock_name_for(code, name)
        cost = qty * price
        self.cash -= cost
        self.used += cost
        # 기존 보유 종목이면 평균가 업데이트
        for h in self.holdings:
            if h.get("code") == code:
                old_qty = h["qty"]
                old_avg = h["avg_price"]
                new_qty = old_qty + qty
                h["avg_price"] = (old_avg * old_qty + price * qty) // new_qty if new_qty > 0 else 0
                h["qty"] = new_qty
                if name and not h.get("name"):
                    h["name"] = name
                return
        self.holdings.append({"code": code, "name": name, "qty": qty, "avg_price": price})

    def sell_holding(self, code: str, qty: int, price: int):
        """매도 기록 + 실현 손익 계산."""
        for h in self.holdings:
            if h.get("code") == code:
                sell_qty = min(qty, h["qty"])
                proceeds = sell_qty * price
                cost_basis = sell_qty * h["avg_price"]
                self.realized_pnl += proceeds - cost_basis
                self.cash += proceeds
                self.used = max(0, self.used - cost_basis)
                h["qty"] -= sell_qty
                if h["qty"] <= 0:
                    self.holdings.remove(h)
                return


@dataclass
class Portfolio:
    total_budget: int = 0
    strategies: dict[str, Strategy] = field(default_factory=dict)

    def allocated(self) -> int:
        return sum(s.budget for s in self.strategies.values())

    def unallocated(self) -> int:
        return max(0, self.total_budget - self.allocated())

    def can_allocate(self, amount: int) -> bool:
        return self.allocated() + amount <= self.total_budget


class StrategyManager:
    """전략 CRUD + 자금 관리."""

    def __init__(self):
        STRATEGIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._portfolio = Portfolio()
        self.refresh()

    def refresh(self):
        """중앙 파일(strategies.json)의 최신 데이터를 다시 읽어 동기화합니다."""
        self._portfolio = self._load()

    def _load(self) -> Portfolio:
        if STRATEGIES_FILE.exists():
            try:
                data = json.loads(STRATEGIES_FILE.read_text(encoding="utf-8"))
                portfolio = Portfolio(total_budget=data.get("total_budget", 0))
                
                # 향후 필드 삭제/변경에도 기존 JSON 호환되도록 필터링
                valid_keys = {f.name for f in fields(Strategy)}
                
                for sd in data.get("strategies", []):
                    # 알려진 필드만 추출
                    filtered_sd = {k: v for k, v in sd.items() if k in valid_keys}
                    s = Strategy(**filtered_sd)
                    portfolio.strategies[s.id] = s
                return portfolio
            except Exception as e:
                logger.error("strategies.json 데이터 로드 실패, 포트폴리오 초기화 진행: %s", e)
        return Portfolio()

    def _save(self):
        data = {
            "total_budget": self._portfolio.total_budget,
            "strategies": [asdict(s) for s in self._portfolio.strategies.values()],
        }
        STRATEGIES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @property
    def portfolio(self) -> Portfolio:
        self.refresh()
        return self._portfolio

    def set_total_budget(self, amount: int):
        self._portfolio.total_budget = amount
        self._save()

    def create_strategy(
        self, name: str, budget: int, description: str = "", max_positions: int = 5
    ) -> Strategy:
        self.refresh()
        if not self._portfolio.can_allocate(budget):
            raise ValueError(
                f"예산 부족: 배정 가능 {self._portfolio.unallocated():,}원, 요청 {budget:,}원"
            )
        sid = f"strat-{len(self._portfolio.strategies) + 1:03d}"
        strategy = Strategy(
            id=sid,
            name=name,
            description=description,
            budget=budget,
            cash=budget,  # 초기 현금 = 배정 예산
            max_positions=max_positions,
            created_at=datetime.now().isoformat(),
        )
        self._portfolio.strategies[sid] = strategy
        self._save()
        logger.info("전략 생성: %s '%s' (예산 %s원)", sid, name, f"{budget:,}")
        return strategy

    def get(self, strategy_id: str) -> Strategy | None:
        self.refresh()
        return self._portfolio.strategies.get(strategy_id)

    def list_strategies(self) -> list[Strategy]:
        self.refresh()
        return list(self._portfolio.strategies.values())

    def update_strategy(self, strategy_id: str, **kwargs) -> Strategy:
        self.refresh()
        s = self._portfolio.strategies.get(strategy_id)
        if not s:
            raise ValueError(f"전략 {strategy_id} 없음")
        for k, v in kwargs.items():
            if hasattr(s, k):
                if k == "budget":
                    diff = v - s.budget
                    if diff > 0 and not self._portfolio.can_allocate(diff):
                        raise ValueError(f"예산 부족: 추가 배정 불가")
                setattr(s, k, v)
        self._save()
        return s

    def delete_strategy(self, strategy_id: str):
        self.refresh()
        if strategy_id in self._portfolio.strategies:
            del self._portfolio.strategies[strategy_id]
            self._save()

    def use_budget(self, strategy_id: str, amount: int) -> bool:
        """전략 예산에서 금액 차감 (주문 시)."""
        self.refresh()
        s = self.get(strategy_id)
        if not s or not s.can_order(amount):
            return False
        s.used += amount
        s.cash -= amount
        self._save()
        return True

    def release_budget(self, strategy_id: str, amount: int):
        """전략 예산 환원 (매도 완료 시)."""
        self.refresh()
        s = self.get(strategy_id)
        if s:
            s.used = max(0, s.used - amount)
            s.cash += amount
            self._save()

    def link_conditions(self, strategy_id: str, conditions_file: str):
        """전략에 조건식 파일 연결."""
        self.refresh()
        s = self.get(strategy_id)
        if s:
            s.conditions_file = conditions_file
            self._save()

    def add_bot(self, strategy_id: str, bot_id: str, ratio: float = 0,
                params: dict = None, auto_rebalance: bool = True):
        """파이프에 봇 추가. auto_rebalance=True면 기존 봇 비율을 자동 축소."""
        s = self.get(strategy_id)
        if not s:
            raise ValueError(f"파이프 {strategy_id} 없음")
        # 중복 체크
        for b in s.bots:
            if b.get("bot_id") == bot_id:
                raise ValueError(f"봇 '{bot_id}' 이미 배정됨")

        if auto_rebalance and s.bots:
            # 0% 봇은 재분배에서 제외 (고정 유지)
            adjustable = [b for b in s.bots if b.get("ratio", 0) > 0]
            frozen_total = sum(b.get("ratio", 0) for b in s.bots if b.get("ratio", 0) == 0)
            available = 1.0 - frozen_total
            new_count = len(adjustable) + 1
            new_bot_ratio = round(available / new_count, 4)
            take_each = round(new_bot_ratio / max(len(adjustable), 1), 4)
            for b in adjustable:
                b["ratio"] = round(b.get("ratio", 0) - take_each, 4)
                if b["ratio"] < 0.01:
                    b["ratio"] = 0.01
            s.bots.append({"bot_id": bot_id, "ratio": new_bot_ratio, "params": params or {}})
            # 합계 보정 (반올림 오차)
            total = sum(b["ratio"] for b in s.bots)
            if abs(total - 1.0) > 0.001:
                diff = round(1.0 - total, 4)
                s.bots[-1]["ratio"] = round(s.bots[-1]["ratio"] + diff, 4)
        else:
            # 수동 비율 지정
            current_total = sum(b.get("ratio", 0) for b in s.bots)
            if ratio <= 0:
                ratio = max(0.01, 1.0 - current_total)
            if current_total + ratio > 1.01:
                raise ValueError(f"비율 초과: 현재 {current_total*100:.0f}% + {ratio*100:.0f}% > 100%")
            s.bots.append({"bot_id": bot_id, "ratio": ratio, "params": params or {}})

        if not s.bot_id:
            s.bot_id = bot_id
        self._save()

    def remove_bot(self, strategy_id: str, bot_id: str):
        """파이프에서 봇 제거."""
        s = self.get(strategy_id)
        if not s:
            return
        s.bots = [b for b in s.bots if b.get("bot_id") != bot_id]
        if s.bot_id == bot_id:
            s.bot_id = s.bots[0]["bot_id"] if s.bots else ""
        self._save()

    def set_bot_ratio(self, strategy_id: str, bot_id: str, ratio: float):
        """봇 투자 비율 변경 (재분배 없음, 단순 변경)."""
        s = self.get(strategy_id)
        if not s:
            return
        for b in s.bots:
            if b.get("bot_id") == bot_id:
                b["ratio"] = ratio
                break
        total = sum(b.get("ratio", 0) for b in s.bots)
        if total > 1.0:
            raise ValueError(f"비율 합 {total*100:.0f}% > 100%")
        self._save()

    def set_bot_ratio_rebalance(self, strategy_id: str, bot_id: str, new_ratio: float):
        """봇 비율 변경 + 나머지 봇 자동 재분배.

        봇 A를 new_ratio로 변경하면 나머지 봇들이 (1 - new_ratio)를
        현재 비율 비례로 자동 재분배된다.
        봇이 1개면 항상 1.0 고정.
        ratio=0인 봇은 재분배에서 제외 (0% 고정 유지).
        """
        s = self.get(strategy_id)
        if not s or not s.bots:
            return
        if len(s.bots) == 1:
            s.bots[0]["ratio"] = 1.0
            self._save()
            return

        new_ratio = max(0.0, min(0.99, new_ratio))

        # 0% 고정 봇들의 비율 합산
        frozen_total = sum(b.get("ratio", 0) for b in s.bots
                          if b.get("bot_id") != bot_id and b.get("ratio", 0) == 0)
        remaining = round(1.0 - new_ratio - frozen_total, 6)

        # 재분배 대상: 변경 봇도 아니고 0%도 아닌 봇
        others = [b for b in s.bots
                  if b.get("bot_id") != bot_id and b.get("ratio", 0) > 0]
        others_total = sum(b.get("ratio", 0) for b in others)

        for b in s.bots:
            if b.get("bot_id") == bot_id:
                b["ratio"] = round(new_ratio, 4)
            elif b.get("ratio", 0) == 0:
                pass  # 0% 고정 유지
            else:
                if others_total > 0:
                    # 기존 비율 비례로 남은 비율 분배
                    b["ratio"] = round(b.get("ratio", 0) / others_total * remaining, 4)
                else:
                    # 나머지를 균등 분배
                    b["ratio"] = round(remaining / len(others), 4)

        self._save()

    def set_bot_interval(self, strategy_id: str, bot_id: str, seconds: int) -> bool:
        """봇 인스턴스의 분석 주기를 params.interval_seconds에 기록.

        ratio·다른 봇 항목은 건드리지 않는다. 봇을 찾으면 True, 없으면 False.
        """
        s = self.get(strategy_id)
        if not s or not s.bots:
            return False
        for b in s.bots:
            if b.get("bot_id") == bot_id:
                params = b.setdefault("params", {})
                params["interval_seconds"] = int(seconds)
                self._save()
                return True
        return False

    def set_bot_ratios_bulk(self, strategy_id: str, ratios: dict[str, float]):
        """봇 비율 일괄 설정. ratios = {bot_id: ratio, ...}. 합계는 1.0이어야 함."""
        s = self.get(strategy_id)
        if not s:
            return
        total = sum(ratios.values())
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"비율 합 {total*100:.1f}% ≠ 100%")
        for b in s.bots:
            bid = b.get("bot_id")
            if bid in ratios:
                b["ratio"] = round(ratios[bid], 4)
        self._save()

    def link_bot(self, strategy_id: str, bot_id: str, bot_params: dict = None):
        """전략에 봇 연결 (단일 봇 — 100% 배정). 복수 봇은 add_bot() 사용."""
        s = self.get(strategy_id)
        if s:
            s.bot_id = bot_id
            s.bot_params = bot_params or {}
            # bots 리스트에도 반영 (기존 제거 후 100%로 추가)
            s.bots = [{"bot_id": bot_id, "ratio": 1.0, "params": bot_params or {}}]
            self._save()

    def record_buy(self, strategy_id: str, code: str, name: str, qty: int, price: int):
        """파이프에 매수 기록."""
        name = stock_name_for(code, name)
        # 종목코드 검증 — 6자리 숫자만 허용
        if not code or not code.isdigit() or len(code) != 6:
            logger.warning("record_buy 차단: 잘못된 종목코드 '%s'", code)
            return
        s = self.get(strategy_id)
        if not s:
            return
        # 입력 검증 — 예산 초과 매수 차단
        cost = qty * price
        if s.budget > 0 and cost > s.budget * 2:  # 예산의 2배 초과는 비정상
            logger.warning(
                "record_buy 차단: %s %d주 @%d = %d원 > 예산 %d의 2배",
                code, qty, price, cost, s.budget,
            )
            return
        if s:
            s.buy_holding(code, name, qty, price)
            s.trades.append({
                "time": datetime.now().isoformat(),
                "side": "buy", "code": code, "qty": qty, "price": price,
            })
            self._save()

    def record_sell(self, strategy_id: str, code: str, qty: int, price: int):
        """파이프에 매도 기록."""
        # 종목코드 검증 — 6자리 숫자만 허용
        if not code or not code.isdigit() or len(code) != 6:
            logger.warning("record_sell 차단: 잘못된 종목코드 '%s'", code)
            return
        s = self.get(strategy_id)
        if s:
            s.sell_holding(code, qty, price)
            s.trades.append({
                "time": datetime.now().isoformat(),
                "side": "sell", "code": code, "qty": qty, "price": price,
            })
            self._save()

    def sync_holdings(self, strategy_id: str, kis_qty_by_code: dict[str, int]):
        """파이프 holdings를 KIS 실잔고로 동기화.

        KIS qty가 0이면 해당 종목 제거. 0이 아니면 qty 갱신 (avg_price는 보존).
        KIS에는 있는데 파이프에 없는 종목은 무시 (외부 매수분 — 봇 관리 영역 아님).
        """
        s = self.get(strategy_id)
        if not s:
            return
        new_holdings = []
        for h in (s.holdings or []):
            code = h.get("code", "")
            kis_qty = int(kis_qty_by_code.get(code, 0) or 0)
            if kis_qty <= 0:
                continue
            new_h = dict(h)
            new_h["qty"] = kis_qty
            new_holdings.append(new_h)
        s.holdings = new_holdings
        self._save()

    def pipe_summary(self, strategy_id: str) -> dict:
        """파이프 자산 요약."""
        s = self.get(strategy_id)
        if not s:
            return {}
        return {
            "id": s.id,
            "name": s.name,
            "budget": s.budget,
            "cash": s.cash,
            "total_value": s.total_value(),
            "profit_rate": s.profit_rate(),
            "holdings": s.holdings,
            "realized_pnl": s.realized_pnl,
            "bot_id": s.bot_id,
            "bots": [{
                "bot_id": b["bot_id"],
                "ratio": b.get("ratio", 0),
                "budget": int(s.budget * b.get("ratio", 0)),
                "params": b.get("params", {}),
                "enabled": b.get("enabled", True),
            } for b in s.bots] if s.bots else [],
            "active": s.active,
            "trade_count": len(s.trades),
        }

"""봇 레지스트리 — 모든 봇의 메타데이터를 체계적으로 관리."""

from dataclasses import dataclass, field
from enum import Enum


class BotCategory(str, Enum):
    """봇 카테고리."""
    MOMENTUM = "모멘텀"       # 추세 추종
    MEAN_REVERT = "역추세"    # 평균 회귀
    ARBITRAGE = "차익"        # 차익 거래
    INCOME = "배당/수익"      # 배당·이자 수익
    MACRO = "매크로"          # 거시경제 기반
    CUSTOM = "커스텀"         # 사용자 정의


class BotType(str, Enum):
    """봇 실행 방식."""
    POLLING = "폴링"          # 주기적 REST 조회
    REALTIME = "실시간"       # WebSocket 기반
    SCHEDULED = "스케줄"      # 특정 시각 실행


class MarketSession(str, Enum):
    """거래 시간대."""
    REGULAR = "정규장"        # 09:00~15:30
    AFTER_HOURS = "시간외"    # 15:40~18:00
    PRE_MARKET = "프리마켓"   # 08:00~09:00
    ALL_DAY = "전시간"        # 위 전체
    US_REGULAR = "미국정규장" # 23:30~06:00 (KST)


@dataclass
class BotParam:
    """봇 설정 파라미터 메타데이터."""
    name: str
    label: str            # 한글 표시명
    type: str             # int, float, str, bool
    default: any
    description: str
    min_value: any = None
    max_value: any = None
    unit: str = ""        # 초, 분, 원, %


class WidgetType(str, Enum):
    """UI 위젯 유형."""
    STAT = "stat"           # 단일 숫자 (예: 수익률 +1.2%)
    TABLE = "table"         # 테이블 (열/행)
    LIST = "list"           # 리스트 (워치리스트 등)
    BAR = "bar"             # 진행 바
    CHART = "chart"         # 미니 차트 (추후)
    TEXT = "text"           # 텍스트 블록
    BADGE = "badge"         # 상태 뱃지


@dataclass
class Widget:
    """봇 UI 위젯 정의."""
    id: str                  # 위젯 식별자 (예: "pnl", "holding", "watchlist")
    type: WidgetType         # 위젯 유형
    label: str               # 표시 제목
    size: str = "1x1"        # 그리드 크기: "1x1", "2x1", "1x2", "2x2"
    data_key: str = ""       # status()에서 가져올 데이터 키 (점 표기: "pnl.total")
    format: str = ""         # 포맷: "won" (원), "pct" (%), "count" (건), "text"
    color_rule: str = ""     # 색상 규칙: "pnl" (양수=green, 음수=red), "status" (running=green)
    columns: list = field(default_factory=list)  # TABLE용 컬럼 정의 [{"key":"code","label":"종목"}]


@dataclass
class BotUISpec:
    """봇 전용 UI 레이아웃 정의."""
    grid_cols: int = 2                    # 그리드 열 수
    widgets: list[Widget] = field(default_factory=list)


@dataclass
class BotInfo:
    """봇 메타데이터."""
    id: str                            # 고유 ID (예: "etf-momentum-top1")
    name: str                          # 표시 이름
    version: str                       # 버전
    category: BotCategory              # 카테고리
    bot_type: BotType                  # 실행 방식
    market_session: MarketSession      # 거래 시간대
    description: str                   # 한 줄 설명
    strategy: str                      # 전략 상세 설명
    risk_level: int                    # 위험도 (1~5)
    params: list[BotParam] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    author: str = ""
    module: str = ""                   # 파이썬 모듈 경로
    ui: BotUISpec = field(default_factory=BotUISpec)  # UI 레이아웃


# ═══════════════════════════════════════════
# 봇 레지스트리
# ═══════════════════════════════════════════

_registry: dict[str, BotInfo] = {}


def register(info: BotInfo):
    """봇을 레지스트리에 등록."""
    _registry[info.id] = info


def get(bot_id: str) -> BotInfo | None:
    """봇 정보 조회."""
    return _registry.get(bot_id)


def interval_spec(bot_id: str) -> dict | None:
    """봇의 interval_seconds 범위/기본값. 미등록 봇/파라미터면 None.

    라우트 검증과 PipeRunner 클램프의 단일 진실원천.
    """
    info = get(bot_id)
    if not info:
        return None
    for p in info.params:
        if p.name == "interval_seconds":
            return {"min": p.min_value, "max": p.max_value, "default": p.default}
    return None


def list_all() -> list[BotInfo]:
    """등록된 모든 봇 목록."""
    return list(_registry.values())


def load_custom_bots():
    """data/bot_registry_custom.json에서 커스텀 봇 로드."""
    import json
    from pathlib import Path
    custom_file = Path(__file__).resolve().parent.parent.parent / "data" / "bot_registry_custom.json"
    if not custom_file.exists():
        return
    try:
        bots = json.loads(custom_file.read_text(encoding="utf-8"))
        for spec in bots:
            cat_map = {"모멘텀": BotCategory.MOMENTUM, "역추세": BotCategory.MEAN_REVERT, "커스텀": BotCategory.CUSTOM,
                       "차익": BotCategory.ARBITRAGE, "배당/수익": BotCategory.INCOME, "매크로": BotCategory.MACRO}
            register(BotInfo(
                id=spec["bot_id"],
                name=spec.get("name", ""),
                version="1.0.0",
                category=cat_map.get(spec.get("category", ""), BotCategory.CUSTOM),
                bot_type=BotType.POLLING,
                market_session=MarketSession.REGULAR,
                description=spec.get("description", ""),
                strategy=spec.get("strategy", ""),
                risk_level=spec.get("risk_level", 3),
                tags=spec.get("tags", []),
                author="AI Builder",
                module=spec.get("module", ""),
                params=[BotParam(**p) for p in spec.get("params", [])] if spec.get("params") else [],
                ui=BotUISpec(widgets=[
                    Widget(id=w["id"], type=WidgetType(w["type"]), label=w["label"],
                           size=w.get("size", "1x1"), data_key=w.get("data_key", ""),
                           format=w.get("format", ""), color_rule=w.get("color_rule", ""),
                           columns=w.get("columns", []))
                    for w in spec.get("ui_widgets", [])
                ]) if spec.get("ui_widgets") else BotUISpec(),
            ))
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("커스텀 봇 로드 실패: %s", e)


def list_by_category(category: BotCategory) -> list[BotInfo]:
    """카테고리별 봇 목록."""
    return [b for b in _registry.values() if b.category == category]


def list_by_session(session: MarketSession) -> list[BotInfo]:
    """거래 시간대별 봇 목록."""
    return [b for b in _registry.values()
            if b.market_session == session or b.market_session == MarketSession.ALL_DAY]


# ═══════════════════════════════════════════
# 기본 봇 등록
# ═══════════════════════════════════════════

# ── 스텔스 볼륨 ──
register(BotInfo(
    id="stealth-volume",
    name="거래량 추적",
    version="2.0.0",
    category=BotCategory.MOMENTUM,
    bot_type=BotType.POLLING,
    market_session=MarketSession.REGULAR,
    description="거래량 3배+ 폭발 감지 → 1분 재확인 후 진입. 트레일링 스탑·일일 거래 제한·장 마감 청산 적용",
    strategy=(
        "30초 간격으로 ETF 분봉 거래량을 모니터링합니다. "
        "평소 대비 거래량 3배 이상 폭발 + 가격 상승 감지 시, "
        "1분 뒤 재확인 후 매수합니다 (가짜 신호 필터). "
        "트레일링 스탑(-0.8%), 익절(+1.5%), 하드 손절(-2%), "
        "하루 최대 3회 매매, 매수 후 최소 20분 보유, "
        "장 마감 10분 전 강제 청산을 적용합니다. "
        "에볼버 봇이 파라미터를 자동 조정합니다."
    ),
    risk_level=3,
    tags=["ETF", "거래량", "폭발", "단타", "트레일링", "에볼버", "국내"],
    author="FF",
    module="src.bots.stealth_volume.StealthVolumeBot",
    params=[
        BotParam("interval_seconds", "스캔 간격", "int", 30, "ETF 스캔 주기", min_value=10, max_value=300, unit="초"),
        BotParam("vol_threshold", "거래량 배율", "float", 3.0, "매수 트리거 거래량 배율", min_value=1.5, max_value=10.0),
        BotParam("trailing_stop_pct", "트레일링 스탑", "float", -0.8, "최고가 대비 손절", min_value=-5.0, max_value=0, unit="%"),
        BotParam("take_profit_pct", "익절선", "float", 1.5, "익절 기준", min_value=0, max_value=10.0, unit="%"),
        BotParam("max_daily_trades", "일일 거래 제한", "int", 3, "하루 최대 매매 횟수", min_value=1, max_value=10),
        BotParam("budget", "투자 예산", "int", 0, "0이면 예수금 전액", min_value=0, unit="원"),
    ],
    ui=BotUISpec(grid_cols=2, widgets=[
        Widget("status", WidgetType.BADGE, "상태", "1x1", "running", color_rule="status"),
        Widget("scan_count", WidgetType.STAT, "스캔 횟수", "1x1", "scan_count", format="count"),
        Widget("realized", WidgetType.STAT, "실현 손익", "1x1", "pnl.realized", format="won", color_rule="pnl"),
        Widget("total_pnl", WidgetType.STAT, "총 손익", "1x1", "pnl.total", format="won", color_rule="pnl"),
        Widget("holding", WidgetType.TEXT, "보유 종목", "2x1", "holding"),
        Widget("scans", WidgetType.LIST, "최근 판단", "2x2", "recent_trades"),
    ]),
))

# ── 미리내 2호 (단타) ──
register(BotInfo(
    id="mirinae-2",
    name="미리내 2호",
    version="1.0.0",
    category=BotCategory.CUSTOM,
    bot_type=BotType.POLLING,
    market_session=MarketSession.REGULAR,
    description="AI 멀티에이전트 단타 트레이딩 — 10분 간격, 거래량+모멘텀 단타, 최대 2시간 보유, 당일 청산",
    strategy=(
        "미리내 1호와 동일한 3단계 AI 토론 구조이지만 단타에 특화. "
        "10분 간격 판단, confidence 0.58 이상 시 실행, "
        "거래량 급증+가격 돌파 종목에 집중, 최대 75분 보유 후 강제 청산, "
        "시장 세션상 매수 불가 전까지 적극 진입하며 하루 10회 매매를 허용합니다."
    ),
    risk_level=4,
    tags=["AI", "멀티에이전트", "단타", "스캘핑", "멀티프로바이더", "ETF", "국내"],
    author="FF",
    module="src.bots.mirinae2.Mirinae2Bot",
    params=[
        BotParam("interval_seconds", "판단 간격", "int", 600, "AI 분석 주기", min_value=300, max_value=1800, unit="초"),
        BotParam("confidence_threshold", "confidence 임계값", "float", 0.58, "이 이상이면 실행", min_value=0.3, max_value=0.9),
        BotParam("max_hold_minutes", "최대 보유 시간", "int", 75, "초과 시 강제 청산", min_value=30, max_value=360, unit="분"),
        BotParam("max_daily_trades", "일일 거래 제한", "int", 10, "하루 최대 매매 횟수", min_value=3, max_value=15),
    ],
    ui=BotUISpec(grid_cols=2, widgets=[
        Widget("status", WidgetType.BADGE, "상태", "1x1", "running", color_rule="status"),
        Widget("scan_count", WidgetType.STAT, "스캔 횟수", "1x1", "scan_count", format="count"),
        Widget("realized", WidgetType.STAT, "실현 손익", "1x1", "pnl.realized", format="won", color_rule="pnl"),
        Widget("total_pnl", WidgetType.STAT, "총 손익", "1x1", "pnl.total", format="won", color_rule="pnl"),
        Widget("positions", WidgetType.TABLE, "보유 포지션", "2x1", "positions", columns=[
            {"key": "code", "label": "종목"},
            {"key": "name", "label": "이름"},
            {"key": "qty", "label": "수량"},
            {"key": "held_min", "label": "보유(분)"},
            {"key": "unrealized", "label": "미실현"},
        ]),
        Widget("debate", WidgetType.LIST, "AI 토론", "2x2", "debate_log"),
        Widget("scans", WidgetType.LIST, "최근 판단", "2x2", "recent_trades"),
    ]),
))

# ── 신규 봇: 미리내 3호 (스윙/자유도 MAX) ──
register(BotInfo(
    id="mirinae-3",
    name="미리내 3호",
    version="1.0.0",
    category=BotCategory.CUSTOM,
    bot_type=BotType.POLLING,
    market_session=MarketSession.REGULAR,
    description="월스트리트 최고 레벨의 적극형 트레이더 — 오버나이트 허용, 자율 스윙/모멘텀 AI",
    strategy=(
        "모든 기계적인 시간 제약(당일 청산, 2시간 강제 청산 등)을 해제하고, "
        "월스트리트 최고 레벨의 적극형 트레이더 판단 하에 매매를 진행합니다. "
        "거시 경제, 펀더멘털, 스윙 타점을 자유롭게 분석하며 오버나이트를 적극 활용합니다. "
        "10분 간격으로 재평가하며, confidence 0.62 이상이면 양수 기대값 포지션을 적극 실행합니다."
    ),
    risk_level=3,
    tags=["AI", "멀티에이전트", "스윙", "오버나이트", "자율형", "ETF", "국내"],
    author="FF",
    module="src.bots.mirinae3.Mirinae3Bot",
    params=[
        BotParam("interval_seconds", "판단 간격", "int", 600, "AI 분석 주기", min_value=300, max_value=7200, unit="초"),
        BotParam("confidence_threshold", "confidence 임계값", "float", 0.62, "이 이상이면 실행", min_value=0.5, max_value=0.9),
    ],
    ui=BotUISpec(grid_cols=2, widgets=[
        Widget("status", WidgetType.BADGE, "상태", "1x1", "running", color_rule="status"),
        Widget("scan_count", WidgetType.STAT, "스캔 횟수", "1x1", "scan_count", format="count"),
        Widget("realized", WidgetType.STAT, "실현 손익", "1x1", "pnl.realized", format="won", color_rule="pnl"),
        Widget("total_pnl", WidgetType.STAT, "총 손익", "1x1", "pnl.total", format="won", color_rule="pnl"),
        Widget("positions", WidgetType.TABLE, "보유 포지션", "2x1", "positions", columns=[
            {"key": "code", "label": "종목"},
            {"key": "name", "label": "이름"},
            {"key": "qty", "label": "수량"},
            {"key": "held_min", "label": "보유(분)"},
            {"key": "unrealized", "label": "미실현"},
        ]),
        Widget("debate", WidgetType.LIST, "AI 판단 근거", "2x2", "debate_log"),
        Widget("scans", WidgetType.LIST, "최근 액션", "2x2", "recent_trades"),
    ]),
))

# ── 신규 봇: Winner 1호 (완전 자율 AI 에이전트) ──
register(BotInfo(
    id="winner-1",
    name="Winner 1호",
    version="1.0.0",
    category=BotCategory.CUSTOM,
    bot_type=BotType.POLLING,
    market_session=MarketSession.REGULAR,
    description="완전 자율 AI 에이전트 — 룰 없이 도구 탐색·자작 도구로 판단",
    strategy=(
        "매 스캔 Claude CLI 세션이 읽기 전용 도구로 시장을 자율 탐색하고, "
        "작업장에 자기 도구를 만들어 축적하며 진입·청산·주기를 전부 스스로 결정. "
        "시스템 개입은 예산·일일손실 한도·파생ETF 토글뿐."
    ),
    risk_level=5,
    tags=["autonomous", "ai", "experimental"],
    author="FF",
    module="src.bots.winner1.Winner1Bot",
    params=[
        BotParam("session_timeout_sec", "세션 타임아웃", "int", 600,
                 "AI 세션 1회 최대 실행 시간", min_value=120, max_value=1800, unit="초"),
        BotParam("min_scan_minutes", "최소 스캔 간격", "int", 5,
                 "에이전트가 정할 수 있는 다음 스캔 하한", min_value=5, max_value=60, unit="분"),
        BotParam("max_scan_minutes", "최대 스캔 간격", "int", 240,
                 "에이전트가 정할 수 있는 다음 스캔 상한", min_value=30, max_value=480, unit="분"),
    ],
    ui=BotUISpec(grid_cols=2, widgets=[
        Widget("status", WidgetType.BADGE, "상태", "1x1", "running", color_rule="status"),
        Widget("scan_count", WidgetType.STAT, "스캔 횟수", "1x1", "scan_count", format="count"),
        Widget("next_scan", WidgetType.STAT, "다음 스캔(분)", "1x1", "next_scan_minutes", format="count"),
        Widget("cash", WidgetType.STAT, "가용 현금", "1x1", "cash", format="won"),
        Widget("realized", WidgetType.STAT, "실현 손익", "1x1", "pnl.realized", format="won", color_rule="pnl"),
        Widget("total_pnl", WidgetType.STAT, "총 손익", "1x1", "pnl.total", format="won", color_rule="pnl"),
        Widget("positions", WidgetType.TABLE, "보유 포지션", "2x1", "positions", columns=[
            {"key": "code", "label": "종목"},
            {"key": "qty", "label": "수량"},
            {"key": "avg_price", "label": "평단"},
            {"key": "unrealized", "label": "미실현"},
        ]),
        Widget("last_reason", WidgetType.TEXT, "최근 판단 근거", "2x1", "last_reason"),
        Widget("scans", WidgetType.LIST, "최근 판단", "2x2", "recent_trades"),
    ]),
))

# ── 신규 봇: 월스트리트 US AI ──
register(BotInfo(
    id="wallstreet-us-ai",
    name="월스트리트 US AI",
    version="1.0.0",
    category=BotCategory.MOMENTUM,
    bot_type=BotType.POLLING,
    market_session=MarketSession.US_REGULAR,
    description="미국주식 전용 AI 에이전트 — 고유동성 메가캡/ETF를 10분 단위로 분석하고 규칙 가드 통과 시 주문",
    strategy=(
        "NVDA, MSFT, AAPL, AMZN, META, GOOGL, TSLA, AVGO, AMD, QQQ, SPY, SOXL, TQQQ 등 "
        "고유동성 미국주식/ETF만 대상으로 합니다. "
        "AI가 가격 모멘텀, 거래량, 뉴스, 섹터 흐름을 비교해 BUY/SELL/HOLD를 결정하고, "
        "코드 기반 가드가 미국장 세션, 워치리스트, confidence 0.62, 손익비 1.5:1, "
        "최대 2포지션, 하루 5회, 포지션당 45% 한도를 최종 검증합니다. "
        "해외주식 포지션은 국내 ETF 파이프 holdings와 섞지 않고 봇 상태에 별도 기록합니다."
    ),
    risk_level=5,
    tags=["AI", "미국주식", "나스닥", "NYSE", "메가캡", "ETF", "모멘텀", "공격형"],
    author="FF",
    module="src.bots.wallstreet_us_ai.WallStreetUSBot",
    params=[
        BotParam("interval_seconds", "판단 간격", "int", 600, "AI 분석 주기", min_value=300, max_value=1800, unit="초"),
        BotParam("confidence_threshold", "confidence 임계값", "float", 0.62, "이 이상이면 실행 검토", min_value=0.5, max_value=0.9),
        BotParam("max_positions", "최대 포지션", "int", 2, "동시 보유 가능한 미국 포지션 수", min_value=1, max_value=5),
        BotParam("max_daily_trades", "일일 거래 제한", "int", 5, "하루 최대 주문 횟수", min_value=1, max_value=10),
        BotParam("max_position_pct", "포지션 최대 비중", "float", 0.45, "한 종목에 배정 가능한 최대 예산 비중", min_value=0.1, max_value=1.0),
        BotParam("allow_min_one_share", "소액 1주 예외", "bool", True, "포지션 비중상 1주가 안 되지만 전체 예산으로 1주가 가능하면 허용"),
        BotParam("take_profit_pct", "익절선", "float", 4.5, "진입가 대비 익절 기준", min_value=1.0, max_value=12.0, unit="%"),
        BotParam("trailing_stop_pct", "트레일링 스탑", "float", -2.2, "고점 대비 청산 기준", min_value=-8.0, max_value=-0.5, unit="%"),
        BotParam("hard_stop_pct", "하드 손절", "float", -3.0, "진입가 대비 절대 손절 기준", min_value=-10.0, max_value=-0.5, unit="%"),
    ],
    ui=BotUISpec(grid_cols=2, widgets=[
        Widget("status", WidgetType.BADGE, "상태", "1x1", "running", color_rule="status"),
        Widget("scan_count", WidgetType.STAT, "스캔 횟수", "1x1", "scan_count", format="count"),
        Widget("realized", WidgetType.STAT, "실현 손익", "1x1", "pnl.realized", format="won", color_rule="pnl"),
        Widget("total_pnl", WidgetType.STAT, "총 손익", "1x1", "pnl.total", format="won", color_rule="pnl"),
        Widget("positions", WidgetType.TABLE, "US 포지션", "2x1", "positions", columns=[
            {"key": "symbol", "label": "티커"},
            {"key": "name", "label": "이름"},
            {"key": "qty", "label": "수량"},
            {"key": "current_price", "label": "현재가"},
            {"key": "unrealized_usd", "label": "미실현($)"},
        ]),
        Widget("debate", WidgetType.LIST, "AI 판단 근거", "2x2", "debate_log"),
        Widget("scans", WidgetType.LIST, "최근 액션", "2x2", "recent_trades"),
    ]),
))

# ── 신규 봇: 시장 분석가 ──
register(BotInfo(
    id="analyst",
    name="시장 분석가",
    version="1.0.0",
    category=BotCategory.CUSTOM,
    bot_type=BotType.POLLING,
    market_session=MarketSession.REGULAR,
    description="매매 없이 차트+뉴스 분석 후 짧은 의견을 게시판에 등록. 주식 커뮤니티 스타일.",
    strategy="15분 간격으로 시세와 뉴스를 분석하여 1~2문장 의견을 게시판에 등록. 다른 봇들이 참고 가능.",
    risk_level=1,
    tags=["의견", "분석", "뉴스", "커뮤니티", "게시판"],
    author="FF",
    module="src.bots.analyst.AnalystBot",
    params=[],
    ui=BotUISpec(grid_cols=2, widgets=[
        Widget("status", WidgetType.BADGE, "상태", "1x1", "running", color_rule="status"),
        Widget("opinion_count", WidgetType.STAT, "오늘 의견", "1x1", "opinion_count", format="count"),
    ]),
))

# ── 신규 봇: 에볼버 ──
register(BotInfo(
    id="evolver",
    name="자동 조율",
    version="1.0.0",
    category=BotCategory.CUSTOM,
    bot_type=BotType.SCHEDULED,
    market_session=MarketSession.ALL_DAY,
    description="1시간마다 봇 1·2의 성과를 분석하고 파라미터를 자동 진화시키는 메타봇",
    strategy=(
        "1시간 스케줄로 실행되어 스텔스 볼륨·밴드 리바운드의 최근 3일 거래 로그를 분석합니다. "
        "승률, 손절/익절 비율, 시간대별 성과를 기반으로 "
        "거래량 배율, 트레일링 스탑, 익절선, BB 기간 등 파라미터를 자동 조정합니다. "
        "각 조정은 ±20% 범위 내로 제한하여 급격한 변화를 방지합니다. "
        "진화 이력은 data/evolution/에 generation 번호와 함께 기록됩니다."
    ),
    risk_level=1,
    tags=["메타봇", "진화", "파라미터 최적화", "자동 조정"],
    author="FF",
    module="src.bots.evolver.Evolver",
    params=[],
    ui=BotUISpec(grid_cols=2, widgets=[
        Widget("generation", WidgetType.STAT, "진화 세대", "1x1", "generation", format="count"),
        Widget("managed", WidgetType.LIST, "관리 봇", "1x1", "managed_bots"),
    ]),
))


# 커스텀 봇 자동 로드
load_custom_bots()

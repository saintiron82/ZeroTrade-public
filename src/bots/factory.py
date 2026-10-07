"""봇 팩토리 — bot_id로 봇 인스턴스를 생성하는 공통 유틸리티.

모든 봇 생성 로직을 이 한 곳에서 관리.
PipeRunner, routes.py, bot_runner.py 모두 이 팩토리를 사용.
"""

import logging

logger = logging.getLogger(__name__)


def create_bot(bot_id: str, budget: int = 0, instance_id: str = "",
               params: dict = None):
    """bot_id로 봇 인스턴스를 생성.

    Args:
        bot_id: 봇 식별자 (예: "stealth-volume", "etf-momentum-top1")
        budget: 배정 예산 (원)
        instance_id: 파이프별 고유 키 (예: "strat-001:stealth-volume")
        params: 봇별 추가 파라미터

    Returns:
        봇 인스턴스 (start/stop/status 메서드 보유) 또는 None
    """
    params = params or {}
    bot = None

    try:
        # ── BotConfig 기반 봇 ──
        if _match(bot_id, "stealth-volume"):
            from src.bots.stealth_volume import StealthVolumeBot
            from src.bots.base import BotConfig
            cfg = BotConfig(
                interval_seconds=params.get("interval_seconds", 30),
                budget=budget,
                instance_id=instance_id,
            )
            bot = StealthVolumeBot(cfg)

        # ── Winner 1호 (완전 자율 AI 에이전트) ──
        elif _match(bot_id, "winner-1"):
            from src.bots.winner1 import Winner1Bot
            from src.bots.base import BotConfig
            cfg = BotConfig(
                interval_seconds=params.get("interval_seconds", 300),
                budget=budget,
                instance_id=instance_id,
            )
            bot = Winner1Bot(cfg)

        # ── 미리내 3호 (스윙/오버나이트) — 2호보다 먼저 매칭 ──
        elif _match(bot_id, "mirinae-3"):
            from src.bots.mirinae3 import Mirinae3Bot
            from src.bots.base import BotConfig
            cfg = BotConfig(
                interval_seconds=params.get("interval_seconds", 600),
                budget=budget,
                instance_id=instance_id,
            )
            bot = Mirinae3Bot(cfg)

        # ── 미리내 2호 (단타 특화) — 1호보다 먼저 매칭 ──
        elif _match(bot_id, "mirinae-2"):
            from src.bots.mirinae2 import Mirinae2Bot
            from src.bots.base import BotConfig
            cfg = BotConfig(
                interval_seconds=params.get("interval_seconds", 600),
                budget=budget,
                instance_id=instance_id,
            )
            bot = Mirinae2Bot(cfg)

        # ── 시장 분석가 (의견 전용) ──
        elif _match(bot_id, "analyst"):
            from src.bots.analyst import AnalystBot
            from src.bots.base import BotConfig
            cfg = BotConfig(
                interval_seconds=params.get("interval_seconds", 900),
                budget=0,
                instance_id=instance_id,
            )
            bot = AnalystBot(cfg)

        # ── 미국주식 AI 에이전트 ──
        elif _match(bot_id, "wallstreet-us-ai"):
            from src.bots.wallstreet_us_ai import WallStreetUSBot
            from src.bots.base import BotConfig
            cfg = BotConfig(
                interval_seconds=params.get("interval_seconds", 600),
                budget=budget,
                instance_id=instance_id,
            )
            bot = WallStreetUSBot(cfg)

        # ── 에볼버 (BotBase 아님, 자체 라이프사이클) ──
        elif _match(bot_id, "evolver"):
            from src.bots.evolver import Evolver
            bot = Evolver()

        # ── 템플릿 봇 (조건 스펙 기반) ──
        else:
            bot = _try_load_template(bot_id, budget, instance_id)
            # 커스텀 봇 fallback (AI 코드 생성)
            if not bot:
                bot = _try_load_custom(bot_id, budget, instance_id)

    except Exception as e:
        logger.error("봇 생성 실패 (%s): %s", bot_id, e, exc_info=True)
        return None

    if bot and params and hasattr(bot, "_params"):
        try:
            bot._params.update(params)
            if "interval_seconds" in params and hasattr(bot, "_config"):
                bot._config.interval_seconds = int(params["interval_seconds"])
        except Exception:
            logger.warning("봇 파라미터 적용 실패: %s", bot_id, exc_info=True)

    if bot:
        logger.info("봇 생성: %s (예산: %d원, 인스턴스: %s)", bot_id, budget, instance_id or "-")
    else:
        logger.warning("알 수 없는 봇: %s", bot_id)

    return bot


def _match(bot_id: str, *keywords: str) -> bool:
    """bot_id가 키워드 중 하나와 정확 매칭 또는 포함 매칭."""
    for kw in keywords:
        if bot_id == kw or kw in bot_id:
            return True
    return False


def _try_load_template(bot_id: str, budget: int, instance_id: str):
    """data/bot_specs/{bot_id}.json 스펙으로 TemplateBot 생성 시도."""
    try:
        from src.bots.template_bot import load_spec, TemplateBot
        from src.bots.base import BotConfig
        spec = load_spec(bot_id)
        if spec:
            cfg = BotConfig(
                interval_seconds=spec.get("interval_seconds", 60),
                budget=budget,
                etf_codes=spec.get("target_codes", []),
                instance_id=instance_id,
            )
            return TemplateBot(spec, cfg)
    except Exception as e:
        logger.warning("템플릿 봇 로드 실패 (%s): %s", bot_id, e)
    return None


def _try_load_custom(bot_id: str, budget: int, instance_id: str):
    """AI 생성 봇(커스텀)을 동적 로드 시도."""
    try:
        from src.bots import registry
        from src.bots.base import BotConfig
        info = registry.get(bot_id)
        if info and info.module:
            parts = info.module.rsplit(".", 1)
            if len(parts) == 2:
                import importlib
                mod = importlib.import_module(parts[0])
                cls = getattr(mod, parts[1])
                cfg = BotConfig(budget=budget, instance_id=instance_id)
                return cls(cfg)
    except Exception as e:
        logger.warning("커스텀 봇 로드 실패 (%s): %s", bot_id, e)
    return None

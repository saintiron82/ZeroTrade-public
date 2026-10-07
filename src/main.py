"""FF Trading Bot — 메인 실행 루프 + CLI."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from src.ai.analyzer import AIAnalyzer, AIProvider
from src.ai.parser import parse_ai_response, save_conditions
from src.ai.prompt import build_analysis_prompt, build_feedback_prompt
from src.config import settings, PROJECT_ROOT
from src.kis.websocket import KISWebSocket, Tick
from src.monitor.evaluator import ConditionEvaluator
from src.monitor.indicators import IndicatorEngine
from src.trade.executor import TradeExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ff")


class TradingBot:
    """FF 자동 투자 봇 메인 클래스."""

    def __init__(self, dry_run: bool = True, provider: AIProvider = AIProvider.CLAUDE, model: str = ""):
        self._ws = KISWebSocket()
        self._indicators = IndicatorEngine()
        self._evaluator = ConditionEvaluator()
        self._executor = TradeExecutor(dry_run=dry_run)
        self._analyzer = AIAnalyzer(provider=provider, model=model)
        self._dry_run = dry_run

    def on_tick(self, tick: Tick):
        """실시간 체결 데이터 수신 콜백."""
        # 지표 업데이트
        ind = self._indicators.update(tick.stock_code, tick.price, tick.volume)

        # 조건 평가
        results = self._evaluator.evaluate(tick.stock_code, ind)
        for result in results:
            if result.triggered:
                logger.info(
                    "조건 충족! %s %s — %s",
                    result.condition.action.upper(),
                    result.condition.stock_name,
                    result.matched_rules,
                )
                self._executor.execute(result)

    async def run(self, conditions_file: str | Path):
        """봇 실행.

        Args:
            conditions_file: AI가 생성한 조건식 JSON 파일 경로
        """
        # 조건식 로드
        self._evaluator.load_from_file(conditions_file)
        stock_codes = self._evaluator.stock_codes()

        if not stock_codes:
            logger.error("감시할 종목이 없습니다. 조건식을 확인하세요.")
            return

        mode = "DRY RUN" if self._dry_run else "LIVE"
        logger.info("FF 봇 시작 [%s] — 감시 종목: %s", mode, stock_codes)

        # WebSocket 콜백 설정
        self._ws.on_tick = self.on_tick

        # WebSocket 연결 및 감시 시작
        try:
            await self._ws.connect(stock_codes)
        except KeyboardInterrupt:
            logger.info("봇 중지 요청")
        finally:
            await self._ws.disconnect()
            self._executor.close()

    def analyze(
        self,
        market_data: dict,
        user_rules: str = "",
        target_stocks: list[str] | None = None,
    ) -> Path | None:
        """AI에게 분석 요청 → 조건식 파일 생성.

        Returns:
            생성된 조건식 파일 경로, 실패 시 None
        """
        trade_history = self._executor.trade_logger.recent(10)

        prompt = build_analysis_prompt(
            market_data=market_data,
            trade_history=trade_history,
            user_rules=user_rules,
            target_stocks=target_stocks,
        )

        logger.info("AI 분석 요청 중...")
        response = self._analyzer.analyze(prompt)
        if not response:
            logger.error("AI 응답 없음")
            return None

        data = parse_ai_response(response)
        if not data:
            logger.error("AI 응답 파싱 실패")
            return None

        path = save_conditions(data)
        logger.info("조건식 생성 완료: %s (%d개 조건)", path, len(data.get("conditions", [])))

        if data.get("analysis"):
            logger.info("AI 분석: %s", data["analysis"])

        return path

    def feedback(self, trade_result: dict, market_data: dict) -> Path | None:
        """매매 결과를 AI에게 피드백 → 조건식 재설계.

        Returns:
            새 조건식 파일 경로
        """
        # 현재 활성 조건식
        current = {
            "conditions": [
                {
                    "id": c.id,
                    "stock_code": c.stock_code,
                    "stock_name": c.stock_name,
                    "action": c.action,
                    "rules": [{"indicator": r.indicator, "operator": r.operator, "value": r.value}
                              for r in c.rules],
                    "logic": c.logic,
                    "amount": c.amount,
                    "reason": c.reason,
                }
                for c in self._evaluator.conditions
            ]
        }

        prompt = build_feedback_prompt(trade_result, current, market_data)
        response = self._analyzer.analyze(prompt)
        if not response:
            return None

        data = parse_ai_response(response)
        if not data:
            return None

        return save_conditions(data)


def setup_mcp_config():
    """현재 실행 중인 Python 경로를 기반으로 .mcp.json 동적 생성."""
    mcp_file = PROJECT_ROOT / ".mcp.json"
    mcp_script = PROJECT_ROOT / "src" / "mcp_server.py"
    
    config = {
        "mcpServers": {
            "ff-trading": {
                "command": sys.executable,
                "args": [str(mcp_script)],
                "env": {
                    "PYTHONPATH": str(PROJECT_ROOT)
                }
            }
        }
    }
    mcp_file.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.debug("MCP 설정 동기화 완료: %s", mcp_file)


def main():
    setup_mcp_config()
    parser = argparse.ArgumentParser(description="FF 자동 투자 봇")
    sub = parser.add_subparsers(dest="command")

    # run: 봇 실행
    run_parser = sub.add_parser("run", help="봇 실행 (조건식 파일 필요)")
    run_parser.add_argument("conditions", help="조건식 JSON 파일 경로")
    run_parser.add_argument("--live", action="store_true", help="실전 매매 모드 (기본: dry run)")
    run_parser.add_argument("--provider", choices=["claude", "codex", "copilot"], default="claude")
    run_parser.add_argument("--model", default="", help="AI 모델 (예: sonnet, opus, haiku, gpt-4.1)")

    # analyze: AI 분석 요청
    analyze_parser = sub.add_parser("analyze", help="AI에게 분석 요청")
    analyze_parser.add_argument("--rules", default="", help="사용자 규칙")
    analyze_parser.add_argument("--stocks", nargs="+", help="분석 대상 종목코드")
    analyze_parser.add_argument("--provider", choices=["claude", "codex", "copilot"], default="claude")
    analyze_parser.add_argument("--model", default="", help="AI 모델")

    # status: 매매 이력 조회
    sub.add_parser("status", help="매매 이력 및 상태 조회")

    # serve: 웹 UI 서버
    serve_parser = sub.add_parser("serve", help="웹 UI 서버 시작")
    serve_parser.add_argument("--port", type=int, default=8080, help="포트")

    # 마이크로서비스 CLI
    sub.add_parser("up", help="전체 시작 (웹 + 활성 파이프)")
    sub.add_parser("down", help="전체 정지")
    sub.add_parser("ps", help="실행 중인 서비스 목록")
    bot_parser = sub.add_parser("bot", help="봇 독립 프로세스 시작")
    bot_parser.add_argument("bot_id", help="봇 ID (etf-momentum-top1 등)")
    pipe_parser = sub.add_parser("pipe", help="파이프 독립 프로세스 시작")
    pipe_parser.add_argument("pipe_id", help="파이프 ID (strat-001 등)")

    args = parser.parse_args()

    if args.command == "run":
        provider = AIProvider(args.provider)
        bot = TradingBot(dry_run=not args.live, provider=provider, model=args.model)
        asyncio.run(bot.run(args.conditions))

    elif args.command == "analyze":
        provider = AIProvider(args.provider)
        bot = TradingBot(provider=provider, model=args.model)
        # 시장 데이터는 추후 REST API로 가져올 예정
        # 지금은 빈 데이터로 AI에게 종목 탐색 요청
        path = bot.analyze(
            market_data={},
            user_rules=args.rules,
            target_stocks=args.stocks,
        )
        if path:
            print(f"조건식 파일 생성: {path}")
        else:
            print("분석 실패", file=sys.stderr)
            sys.exit(1)

    elif args.command == "status":
        from src.trade.logger import TradeLogger

        tl = TradeLogger()
        summary = tl.summary()
        print(f"총 매매: {summary['total']}건 (매수 {summary['buys']}, 매도 {summary['sells']})")
        print(f"성공률: {summary['success_rate']:.0%}")
        recent = tl.recent(5)
        if recent:
            print("\n최근 매매:")
            for t in recent:
                status = "O" if t["success"] else "X"
                print(f"  [{status}] {t['timestamp'][:16]} {t['side']} {t['stock_code']} {t['quantity']}주")

    elif args.command == "serve":
        from src.web.app import serve
        serve(port=args.port)

    elif args.command == "up":
        from src.services.orchestrator import Orchestrator
        orch = Orchestrator()
        orch.start_all(port=args.port if hasattr(args, 'port') else 8080)

    elif args.command == "down":
        from src.services.orchestrator import Orchestrator
        Orchestrator().stop_all()

    elif args.command == "ps":
        from src.services.orchestrator import Orchestrator
        services = Orchestrator().status()
        if not services:
            print("실행 중인 서비스 없음")
        else:
            print(f"{'TYPE':<8} {'ID':<20} {'PID':<8} {'STATUS':<8}")
            print("-" * 48)
            for s in services:
                alive = "ALIVE" if s["alive"] else "DEAD"
                print(f"{s['type']:<8} {s['id']:<20} {s['pid']:<8} {alive:<8}")

    elif args.command == "bot":
        from src.services.orchestrator import Orchestrator
        Orchestrator().start_bot(args.bot_id)

    elif args.command == "pipe":
        from src.services.orchestrator import Orchestrator
        Orchestrator().start_pipe(args.pipe_id)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()

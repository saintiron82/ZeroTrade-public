"""FastAPI 앱 — 웹 서버 엔트리포인트."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from src.bot_manager import get_bot_manager
from src.web.routes import router
from src.web.sse import broker

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    """서버 시작 시 활성 전략 자동 복구."""
    mgr = get_bot_manager()
    await mgr.auto_restore()
    if mgr._runners:
        await broker.publish("system", {
            "message": f"자동 복구: {len(mgr._runners)}개 전략 활성화"
        })

    # 활성 파이프 자동 복구 (독립 프로세스)
    # 실거래 환경에서는 서버 재시작만으로 주문 판단 프로세스가 살아나면 위험하다.
    # 명시적으로 FF_AUTO_RESTORE_PIPES=1 을 준 경우에만 복구한다.
    try:
        import os as _os
        from src.services.orchestrator import Orchestrator
        from src.strategy import StrategyManager
        if _os.getenv("FF_AUTO_RESTORE_PIPES", "0") == "1":
            orch = Orchestrator()
            strat_mgr = StrategyManager()
            restored_pipes = 0
            for s in strat_mgr.list_strategies():
                if s.active and s.bots:
                    pid_file = PROJECT_ROOT / "data" / "pids" / f"pipe_{s.id}.pid"
                    already_running = False
                    if pid_file.exists():
                        try:
                            pid = int(pid_file.read_text().strip())
                            _os.kill(pid, 0)
                            already_running = True
                        except (ProcessLookupError, ValueError):
                            pid_file.unlink(missing_ok=True)
                    if not already_running:
                        orch.start_pipe(s.id)
                        restored_pipes += 1
            if restored_pipes:
                logger.info("파이프 자동 복구: %d개", restored_pipes)
                await broker.publish("system", {"message": f"파이프 자동 복구: {restored_pipes}개"})
        else:
            logger.info("파이프 자동 복구 비활성화 (FF_AUTO_RESTORE_PIPES=1 필요)")
    except Exception as e:
        logger.warning("파이프 자동 복구 실패: %s", e)

    # 뉴스 수집기 시작
    from src.news.collector import NewsCollector
    news = NewsCollector(interval_seconds=600)
    await news.start()
    logger.info("뉴스 수집기 시작")

    yield

    # 서버 종료 시
    await news.stop()  # 내부 봇 + 독립 프로세스 모두 정리
    await mgr.stop()
    try:
        from src.services.orchestrator import Orchestrator
        orch = Orchestrator()
        orch.stop_all()
        logger.info("독립 프로세스 전체 정지 완료")
    except Exception as e:
        logger.warning("독립 프로세스 정지 실패: %s", e)


app = FastAPI(title="ZeroTrade", version="0.2.0", lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/bots")
async def bots_viewer():
    return FileResponse(str(STATIC_DIR / "bots.html"))


@app.get("/bots/{bot_id}")
async def bot_detail(bot_id: str):
    return FileResponse(str(STATIC_DIR / "bot-detail.html"))


@app.get("/vwap")
async def vwap_viewer():
    return FileResponse(str(STATIC_DIR / "etf-vwap-bounce.html"))


@app.get("/news")
async def news_viewer():
    return FileResponse(str(STATIC_DIR / "news.html"))


@app.get("/bot-designer")
async def bot_designer():
    return FileResponse(str(STATIC_DIR / "bot-designer.html"))


@app.get("/mirinae")
async def mirinae_viewer():
    return FileResponse(str(STATIC_DIR / "mirinae.html"))

@app.get("/mirinae2")
async def mirinae2_viewer():
    return FileResponse(str(STATIC_DIR / "mirinae2.html"))


@app.get("/sectors")
async def sectors_viewer():
    return FileResponse(str(STATIC_DIR / "sectors.html"))


@app.get("/pipes")
async def pipes_viewer():
    return FileResponse(str(STATIC_DIR / "pipes.html"))


@app.get("/logs")
async def logs_viewer():
    return FileResponse(str(STATIC_DIR / "logs.html"))


@app.get("/trades")
async def trades_viewer():
    return FileResponse(str(STATIC_DIR / "trades.html"))


@app.get("/performance")
async def performance_viewer():
    return FileResponse(str(STATIC_DIR / "performance.html"))


def serve(host: str = "0.0.0.0", port: int = 8080):
    """웹 서버 시작."""
    import uvicorn
    logger.info("FF 웹 서버 시작: http://localhost:%d", port)
    uvicorn.run(app, host=host, port=port, log_level="info")

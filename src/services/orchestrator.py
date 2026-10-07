"""Orchestrator — 모든 서비스 관리 (시작/정지/모니터링).

Usage:
    python -m src.services.orchestrator start   # 전체 시작
    python -m src.services.orchestrator stop    # 전체 정지
    python -m src.services.orchestrator ps      # 상태 확인
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.services.redis_bus import get_bus
from src.strategy import StrategyManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("orchestrator")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PYTHON = str(PROJECT_ROOT / ".venv" / "bin" / "python")
PID_DIR = PROJECT_ROOT / "data" / "pids"


class Orchestrator:
    """서비스 프로세스 관리."""

    def __init__(self):
        PID_DIR.mkdir(parents=True, exist_ok=True)
        self.bus = get_bus()

    def start_web(self, port: int = 8080):
        """웹 서버 시작."""
        return self._spawn("web", "main", [PYTHON, "-m", "src.main", "serve", "--port", str(port)])

    def start_bot(self, bot_id: str, config: dict = None):
        """봇 독립 프로세스 시작."""
        args = [PYTHON, "-m", "src.services.bot_runner", bot_id]
        if config:
            args.append(json.dumps(config))
        return self._spawn("bot", bot_id, args)

    def start_pipe(self, pipe_id: str):
        """파이프 독립 프로세스 시작."""
        return self._spawn("pipe", pipe_id, [PYTHON, "-m", "src.services.pipe_runner", pipe_id])

    def start_all(self, port: int = 8080):
        """전체 시작: 웹 + 활성 파이프 + 활성 봇."""
        started = []

        # 1. 웹 서버
        pid = self.start_web(port)
        if pid:
            started.append(f"web (PID: {pid})")

        # 2. 활성 파이프
        mgr = StrategyManager()
        for s in mgr.list_strategies():
            if s.active and s.bot_id:
                pid = self.start_pipe(s.id)
                if pid:
                    started.append(f"pipe:{s.id} '{s.name}' (PID: {pid})")

        logger.info("전체 시작 완료: %d개 서비스", len(started))
        for s in started:
            logger.info("  %s", s)

    def stop_all(self):
        """전체 정지."""
        for pid_file in PID_DIR.glob("*.pid"):
            pid = int(pid_file.read_text().strip())
            name = pid_file.stem
            try:
                os.kill(pid, signal.SIGTERM)
                logger.info("정지: %s (PID: %d)", name, pid)
            except ProcessLookupError:
                pass
            pid_file.unlink()

    def stop_service(self, service_type: str, service_id: str):
        """특정 서비스 정지."""
        pid_file = PID_DIR / f"{service_type}_{service_id}.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            try:
                os.kill(pid, signal.SIGTERM)
                logger.info("정지: %s:%s (PID: %d)", service_type, service_id, pid)
            except ProcessLookupError:
                pass
            pid_file.unlink()
        # Redis로도 명령 전달
        self.bus.send_command(service_type, service_id, "stop")

    def status(self) -> list[dict]:
        """실행 중인 서비스 상태."""
        services = []
        for pid_file in PID_DIR.glob("*.pid"):
            pid = int(pid_file.read_text().strip())
            name = pid_file.stem
            parts = name.split("_", 1)
            stype = parts[0]
            sid = parts[1] if len(parts) > 1 else ""

            # 프로세스 살아있는지 확인
            alive = self._is_alive(pid)

            # Redis 상태
            redis_status = self.bus.get_status(stype, sid) if sid else None

            services.append({
                "type": stype,
                "id": sid,
                "pid": pid,
                "alive": alive,
                "status": redis_status,
            })

            # 죽은 프로세스 정리
            if not alive:
                pid_file.unlink()

        return services

    def _spawn(self, service_type: str, service_id: str, args: list) -> int | None:
        """프로세스 생성."""
        pid_file = PID_DIR / f"{service_type}_{service_id}.pid"

        # 이미 실행 중인지 확인 — PID 생존만으로는 부족하다.
        # 프로세스가 죽은 뒤 OS가 같은 PID를 다른 프로세스에 재사용하면
        # os.kill(pid, 0)이 True가 되어 시작 요청이 조용히 무시된다 (2026-06-12 실사고).
        # 명령줄에 우리 서비스 모듈이 있는지까지 확인한다.
        if pid_file.exists():
            old_pid = int(pid_file.read_text().strip())
            if self._is_alive(old_pid) and self._cmdline_matches(old_pid, args):
                logger.warning("%s:%s 이미 실행 중 (PID: %d)", service_type, service_id, old_pid)
                return old_pid
            logger.info("%s:%s 죽은/재사용된 PID 파일 정리 (PID: %d)",
                        service_type, service_id, old_pid)
            pid_file.unlink(missing_ok=True)

        # 프로세스 시작 — 출력을 로그 파일로 보존.
        # DEVNULL로 버리면 파이프가 기동 직후 죽어도 원인이 어디에도 안 남는다
        # (2026-06-12: 14:05 재시작 후 봇들이 조용히 멈췄는데 로그 부재로 원인 미상).
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(log_dir / f"{service_type}_{service_id}.out", "a")
        log_file.write(f"\n===== spawn {service_type}:{service_id} =====\n")
        log_file.flush()
        proc = subprocess.Popen(
            args,
            cwd=str(PROJECT_ROOT),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_file.close()
        pid_file.write_text(str(proc.pid))
        logger.info("시작: %s:%s (PID: %d)", service_type, service_id, proc.pid)
        return proc.pid

    def _is_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _cmdline_matches(self, pid: int, args: list) -> bool:
        """PID의 실제 명령줄이 우리가 띄운 서비스인지 확인 (PID 재사용 방어)."""
        try:
            out = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            # 모듈 경로(예: src.services.pipe_runner)와 서비스 인자(예: strat-001)가
            # 명령줄에 모두 있어야 우리 프로세스로 인정
            markers = [a for a in args if a.startswith("src.") or not a.startswith("-")]
            return bool(out) and all(m in out for m in markers[-2:])
        except Exception:
            return False


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m src.services.orchestrator <command>")
        print("  start   — 전체 시작 (웹 + 활성 파이프)")
        print("  stop    — 전체 정지")
        print("  ps      — 실행 상태")
        print("  bot <id>  — 봇 시작")
        print("  pipe <id> — 파이프 시작")
        sys.exit(1)

    orch = Orchestrator()
    cmd = sys.argv[1]

    if cmd == "start":
        orch.start_all()
    elif cmd == "stop":
        orch.stop_all()
    elif cmd == "ps":
        services = orch.status()
        if not services:
            print("실행 중인 서비스 없음")
        else:
            print(f"{'TYPE':<8} {'ID':<20} {'PID':<8} {'STATUS':<8}")
            print("-" * 48)
            for s in services:
                alive = "ALIVE" if s["alive"] else "DEAD"
                print(f"{s['type']:<8} {s['id']:<20} {s['pid']:<8} {alive:<8}")
    elif cmd == "bot" and len(sys.argv) > 2:
        orch.start_bot(sys.argv[2])
    elif cmd == "pipe" and len(sys.argv) > 2:
        orch.start_pipe(sys.argv[2])
    else:
        print(f"알 수 없는 명령: {cmd}")


if __name__ == "__main__":
    main()

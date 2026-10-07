#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate

PORT=8080
existing_pids=$(lsof -nP -tiTCP:${PORT} -sTCP:LISTEN 2>/dev/null)
if [ -n "$existing_pids" ]; then
  echo "Port ${PORT} is in use by PID(s): $existing_pids — stopping..."
  kill $existing_pids 2>/dev/null
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.5
    still=$(lsof -nP -tiTCP:${PORT} -sTCP:LISTEN 2>/dev/null)
    [ -z "$still" ] && break
  done
  still=$(lsof -nP -tiTCP:${PORT} -sTCP:LISTEN 2>/dev/null)
  if [ -n "$still" ]; then
    echo "PID(s) $still still alive, sending SIGKILL..."
    kill -9 $still 2>/dev/null
    sleep 0.5
  fi
  if lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ERROR: failed to free port ${PORT}:"
    lsof -nP -iTCP:${PORT} -sTCP:LISTEN
    exit 1
  fi
  echo "Port ${PORT} freed."
fi

# 잔여 파이프/봇 프로세스 정리 — 새 orchestrator는 이전 세션의 PID를 추적하지
# 못하므로, 여기서 정리하지 않으면 구버전 코드로 계속 거래하는 잔여 파이프가 남는다
# (2026-06-11 실사고: 재시작에서 살아남은 파이프가 ratio 0% 봇으로 매수까지 실행).
leftover=$(pgrep -f "src.services.(pipe_runner|bot_runner)" 2>/dev/null)
if [ -n "$leftover" ]; then
  echo "Leftover pipe/bot process(es): $leftover — stopping..."
  kill $leftover 2>/dev/null
  sleep 2
  still=$(pgrep -f "src.services.(pipe_runner|bot_runner)" 2>/dev/null)
  [ -n "$still" ] && kill -9 $still 2>/dev/null
fi

trap 'kill $(jobs -p) 2>/dev/null; exit 0' INT TERM
python -m src.main serve --port ${PORT} &
wait

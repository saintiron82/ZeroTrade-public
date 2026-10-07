#!/bin/bash
# ═══════════════════════════════════════════
# ZeroTrade 배포 — 개발 → 운영 코드 동기화
# .env, data/, .venv 등은 제외 (운영 데이터 보호)
# ═══════════════════════════════════════════

SRC="$HOME/Projects/FF/"
DST="$HOME/Services/ZeroTrade/"

echo "═══ ZeroTrade 배포 ═══"
echo "소스: $SRC"
echo "대상: $DST"
echo ""

# 코드만 동기화 (데이터/설정/캐시 제외)
rsync -av --delete \
  --exclude='.env' \
  --exclude='data/' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.git/' \
  --exclude='.kis_token.json' \
  --exclude='.kis_real_token.json' \
  --exclude='.pytest_cache/' \
  --exclude='.ruff_cache/' \
  --exclude='logs/' \
  "$SRC" "$DST"

echo ""
echo "═══ 배포 완료 ═══"
echo "운영 서버 시작: cd $DST && ./start.sh"

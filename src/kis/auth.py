"""KIS OpenAPI 인증 — 토큰 발급 및 자동 갱신."""

import json
import time
from pathlib import Path

import httpx

from src.config import settings, PROJECT_ROOT

TOKEN_CACHE_FILE = PROJECT_ROOT / ".kis_token.json"
TOKEN_EXPIRE_MARGIN = 3600  # 만료 1시간 전에 갱신


class KISAuth:
    """KIS OpenAPI 인증 토큰 관리.

    - access_token 발급 및 캐싱
    - 만료 전 자동 갱신
    - WebSocket용 approval_key 발급
    """

    def __init__(self):
        self._cfg = settings.kis
        self._client = httpx.Client(base_url=self._cfg.base_url, timeout=10)
        self._token: str | None = None
        self._token_expires_at: float = 0
        self._approval_key: str | None = None
        self._load_cached_token()

    def _load_cached_token(self):
        if TOKEN_CACHE_FILE.exists():
            try:
                data = json.loads(TOKEN_CACHE_FILE.read_text())
                if data.get("expires_at", 0) > time.time() + TOKEN_EXPIRE_MARGIN:
                    self._token = data["access_token"]
                    self._token_expires_at = data["expires_at"]
            except (json.JSONDecodeError, KeyError):
                pass

    def _save_token(self, token: str, expires_at: float):
        TOKEN_CACHE_FILE.write_text(
            json.dumps({"access_token": token, "expires_at": expires_at})
        )

    @property
    def access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - TOKEN_EXPIRE_MARGIN:
            return self._token
        self._issue_token()
        return self._token

    def _issue_token(self):
        resp = self._client.post(
            "/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self._cfg.app_key,
                "appsecret": self._cfg.app_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        # KIS 토큰은 24시간 유효
        self._token_expires_at = time.time() + 86400
        self._save_token(self._token, self._token_expires_at)

    @property
    def approval_key(self) -> str:
        """WebSocket 접속용 approval_key 발급."""
        if self._approval_key:
            return self._approval_key
        resp = self._client.post(
            "/oauth2/Approval",
            json={
                "grant_type": "client_credentials",
                "appkey": self._cfg.app_key,
                "secretkey": self._cfg.app_secret,
            },
        )
        resp.raise_for_status()
        self._approval_key = resp.json()["approval_key"]
        return self._approval_key

    @property
    def headers(self) -> dict:
        """API 호출용 공통 헤더."""
        return {
            "authorization": f"Bearer {self.access_token}",
            "appkey": self._cfg.app_key,
            "appsecret": self._cfg.app_secret,
            "Content-Type": "application/json; charset=utf-8",
        }

    def close(self):
        self._client.close()

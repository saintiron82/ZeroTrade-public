"""KIS 인증 모듈 테스트."""

import json
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("KIS_APP_KEY", "test_key")
    monkeypatch.setenv("KIS_APP_SECRET", "test_secret")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "50012345-01")
    monkeypatch.setenv("KIS_IS_PAPER", "true")


def test_auth_headers_contain_required_fields(mock_env):
    with patch.object(httpx, "Client") as MockClient:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "test_token_123",
            "token_type": "Bearer",
        }
        mock_resp.raise_for_status = MagicMock()
        MockClient.return_value.post.return_value = mock_resp

        from src.kis.auth import KISAuth, TOKEN_CACHE_FILE

        # 캐시 파일이 있으면 제거
        if TOKEN_CACHE_FILE.exists():
            TOKEN_CACHE_FILE.unlink()

        auth = KISAuth()
        headers = auth.headers

        assert "authorization" in headers
        assert "appkey" in headers
        assert "appsecret" in headers
        assert headers["authorization"].startswith("Bearer ")


def test_token_caching(mock_env, tmp_path):
    """토큰이 캐시 파일에 저장되는지 확인."""
    cache_file = tmp_path / ".kis_token.json"
    cache_data = {
        "access_token": "cached_token",
        "expires_at": time.time() + 86400,
    }
    cache_file.write_text(json.dumps(cache_data))

    with patch("src.kis.auth.TOKEN_CACHE_FILE", cache_file), \
         patch.object(httpx, "Client"):
        from src.kis.auth import KISAuth

        auth = KISAuth()
        assert auth._token == "cached_token"

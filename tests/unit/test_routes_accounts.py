"""Tests for accounts routes, cache isolation headers, and /api/accounts prefix."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.account.account_store import AccountMeta, AccountStore


@pytest.fixture(autouse=True)
def setup_mock_account_service():
    mock_svc = MagicMock(spec=AccountService)
    sample_acc = AccountMeta(
        id="acc_1",
        name="Test Account 1",
        email="test1@gmail.com",
        created_at="2026-10-17T00:00:00Z",
        last_used=None,
        auth_user="0",
        cookie_id="cookie_1",
    )
    mock_svc.list_accounts.return_value = [sample_acc]
    mock_svc.get_active_account.return_value = sample_acc

    orig = runtime_state.account_service
    runtime_state.account_service = mock_svc
    try:
        yield mock_svc
    finally:
        runtime_state.account_service = orig


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_accounts_html_navigation_headers():
    """Verify that visiting /accounts as HTML returns the SPA HTML with cache isolation headers."""
    async with _client() as client:
        for path in ("/accounts", "/accounts/"):
            resp = await client.get(
                path, headers={"Accept": "text/html,application/xhtml+xml"}
            )
            assert resp.status_code == 200
            assert "<!DOCTYPE html>" in resp.text
            assert "text/html" in resp.headers.get("Content-Type", "")
            assert "Accept" in resp.headers.get("Vary", "")
            assert "no-store" in resp.headers.get("Cache-Control", "")


@pytest.mark.asyncio
async def test_accounts_json_api_headers():
    """Verify that calling /accounts with Accept: application/json returns JSON with cache isolation headers."""
    async with _client() as client:
        resp = await client.get("/accounts", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        assert "application/json" in resp.headers.get("Content-Type", "")
        assert "Accept" in resp.headers.get("Vary", "")
        assert "no-store" in resp.headers.get("Cache-Control", "")
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == "acc_1"


@pytest.mark.asyncio
async def test_api_accounts_endpoint_headers():
    """Verify that the dedicated /api/accounts endpoint returns JSON with cache isolation headers."""
    async with _client() as client:
        for path in ("/api/accounts", "/api/accounts/"):
            resp = await client.get(path, headers={"Accept": "application/json"})
            assert resp.status_code == 200
            assert "application/json" in resp.headers.get("Content-Type", "")
            assert "Accept" in resp.headers.get("Vary", "")
            assert "no-store" in resp.headers.get("Cache-Control", "")
            data = resp.json()
            assert isinstance(data, list)
            assert len(data) == 1
            assert data[0]["id"] == "acc_1"


@pytest.mark.asyncio
async def test_api_accounts_and_legacy_accounts_parity():
    """Verify that /api/accounts and /accounts return identical payload for JSON clients."""
    async with _client() as client:
        resp_legacy = await client.get(
            "/accounts", headers={"Accept": "application/json"}
        )
        resp_api = await client.get(
            "/api/accounts", headers={"Accept": "application/json"}
        )
        assert resp_legacy.status_code == 200
        assert resp_api.status_code == 200
        assert resp_legacy.json() == resp_api.json()


@pytest.mark.asyncio
async def test_issue_5_cache_isolation_regression():
    """Regression test for Issue #5:
    Ensure that a client requesting HTML followed by JSON (or vice-versa) receives distinct
    content types and both responses carry Vary: Accept and Cache-Control: no-store headers.
    """
    async with _client() as client:
        html_resp = await client.get("/accounts", headers={"Accept": "text/html"})
        assert html_resp.status_code == 200
        assert "<!DOCTYPE html>" in html_resp.text
        assert "Accept" in html_resp.headers.get("Vary", "")
        assert "no-store" in html_resp.headers.get("Cache-Control", "")

        json_resp = await client.get(
            "/accounts", headers={"Accept": "application/json"}
        )
        assert json_resp.status_code == 200
        assert "application/json" in json_resp.headers.get("Content-Type", "")
        assert "Accept" in json_resp.headers.get("Vary", "")
        assert "no-store" in json_resp.headers.get("Cache-Control", "")
        assert isinstance(json_resp.json(), list)


@pytest.mark.asyncio
async def test_api_accounts_active_endpoint():
    """Verify subroutes under /api/accounts like /api/accounts/active work."""
    async with _client() as client:
        resp = await client.get(
            "/api/accounts/active", headers={"Accept": "application/json"}
        )
        assert resp.status_code == 200
        assert "Accept" in resp.headers.get("Vary", "")
        assert "no-store" in resp.headers.get("Cache-Control", "")
        data = resp.json()
        assert data["id"] == "acc_1"


@pytest.mark.asyncio
async def test_accounts_health_check_endpoint(monkeypatch):
    """Verify POST /api/accounts/health-check returns the probe summary."""
    from aistudio_api.application import account_health

    async def fake_check(_account_service, _rotator):
        return {
            "checked": 1,
            "valid": 0,
            "expired": ["acc_1"],
            "unknown": [],
        }

    monkeypatch.setattr(account_health, "check_all_accounts", fake_check)
    original_rotator = runtime_state.rotator
    runtime_state.rotator = MagicMock()
    try:
        async with _client() as client:
            resp = await client.post("/api/accounts/health-check")
            assert resp.status_code == 200
            assert resp.json() == {
                "checked": 1,
                "valid": 0,
                "expired": ["acc_1"],
                "unknown": [],
            }
    finally:
        runtime_state.rotator = original_rotator


@pytest.mark.asyncio
async def test_delete_expired_accounts_only_removes_session_expired():
    """DELETE /api/accounts/expired 只删除登录态失效账号，不影响正常账号。"""
    expired = AccountMeta(
        id="acc_expired",
        name="Expired Account",
        email="expired@example.com",
        created_at="2026-10-17T00:00:00Z",
        last_used=None,
        auth_user="0",
        cookie_id="cookie_1",
    )
    healthy = AccountMeta(
        id="acc_healthy",
        name="Healthy Account",
        email="healthy@example.com",
        created_at="2026-10-17T00:00:00Z",
        last_used=None,
        auth_user="0",
        cookie_id="cookie_2",
    )

    mock_svc = MagicMock(spec=AccountService)
    mock_svc.list_accounts.return_value = [expired, healthy]
    mock_svc.get_active_account.return_value = healthy
    mock_svc.delete_account.side_effect = lambda account_id: account_id == expired.id

    expired_stats = MagicMock(session_expired=True)
    healthy_stats = MagicMock(session_expired=False)
    rotator = MagicMock()
    rotator.get_account_stats.side_effect = lambda account_id: (
        expired_stats if account_id == expired.id else healthy_stats
    )

    original_service = runtime_state.account_service
    original_rotator = runtime_state.rotator
    original_client = runtime_state.client
    runtime_state.account_service = mock_svc
    runtime_state.rotator = rotator
    runtime_state.client = None
    try:
        async with _client() as client:
            resp = await client.delete("/api/accounts/expired")
        assert resp.status_code == 200
        assert resp.json() == {
            "deleted": 1,
            "ids": [expired.id],
            "names": [expired.name],
        }
        mock_svc.delete_account.assert_called_once_with(expired.id)
        rotator.remove_account.assert_called_once_with(expired.id)
    finally:
        runtime_state.account_service = original_service
        runtime_state.rotator = original_rotator
        runtime_state.client = original_client


@pytest.mark.asyncio
async def test_delete_expired_accounts_noop_when_none_expired():
    """没有失效账号时返回 deleted=0，且不会误删任何账号。"""
    acc = AccountMeta(
        id="acc_ok",
        name="Healthy Account",
        email="ok@example.com",
        created_at="2026-10-17T00:00:00Z",
        last_used=None,
        auth_user="0",
        cookie_id="cookie_1",
    )
    mock_svc = MagicMock(spec=AccountService)
    mock_svc.list_accounts.return_value = [acc]

    rotator = MagicMock()
    rotator.get_account_stats.return_value = MagicMock(session_expired=False)

    original_service = runtime_state.account_service
    original_rotator = runtime_state.rotator
    original_client = runtime_state.client
    runtime_state.account_service = mock_svc
    runtime_state.rotator = rotator
    runtime_state.client = None
    try:
        async with _client() as client:
            resp = await client.delete("/api/accounts/expired")
        assert resp.status_code == 200
        assert resp.json() == {"deleted": 0, "ids": [], "names": []}
        mock_svc.delete_account.assert_not_called()
    finally:
        runtime_state.account_service = original_service
        runtime_state.rotator = original_rotator
        runtime_state.client = original_client


@pytest.mark.asyncio
async def test_detect_email_conflict_requires_confirmation(tmp_path, monkeypatch):
    """探测到的邮箱与记录不一致时不得静默覆盖，返回冲突交由前端确认。"""
    store = AccountStore(accounts_dir=tmp_path / "accounts_detect")
    acc = store.save_account(
        name="{{EMAIL_xzq5pepq}}",
        email="{{EMAIL_xzq5pepq}}",
        storage_state={"cookies": [{"name": "SID", "value": "sid"}]},
    )
    service = AccountService(store)

    async def fake_email(_cookies, auth_user="0"):
        return "{{EMAIL_wsj3yhqf}}"

    async def fake_tier(_cookies, auth_user="0"):
        return "Pro"

    monkeypatch.setattr(
        "aistudio_api.api.routes_accounts.probe_account_email", fake_email
    )
    monkeypatch.setattr(
        "aistudio_api.api.routes_accounts.probe_account_benefit_tier", fake_tier
    )

    original_service = runtime_state.account_service
    original_client = runtime_state.client
    runtime_state.account_service = service
    runtime_state.client = None
    try:
        async with _client() as client:
            resp = await client.post(f"/api/accounts/{acc.id}/detect-email")

            assert resp.status_code == 200
            data = resp.json()
            assert data["detected_email"] == "{{EMAIL_wsj3yhqf}}".lower()
            assert data["email_conflict"] is True
            assert data["email_updated"] is False
            # 未确认时保留原值
            assert data["email"] == "{{EMAIL_xzq5pepq}}"

            forced = await client.post(
                f"/api/accounts/{acc.id}/detect-email?force=true"
            )

            assert forced.status_code == 200
            forced_data = forced.json()
            assert forced_data["email_conflict"] is False
            assert forced_data["email_updated"] is True
            assert forced_data["email"] == "{{EMAIL_wsj3yhqf}}".lower()
            # 名称当初等于旧邮箱，应跟随邮箱一起更新
            assert forced_data["name"] == "{{EMAIL_wsj3yhqf}}".lower()
    finally:
        runtime_state.account_service = original_service
        runtime_state.client = original_client


@pytest.mark.asyncio
async def test_detect_email_never_reads_unbound_browser_session(tmp_path, monkeypatch):
    """浏览器会话未绑定该账号时，不允许从当前页面提取邮箱。"""
    store = AccountStore(accounts_dir=tmp_path / "accounts_unbound")
    acc = store.save_account(
        name="account",
        email=None,
        storage_state={"cookies": [{"name": "SID", "value": "sid"}]},
    )
    service = AccountService(store)

    async def fake_email(_cookies, auth_user="0"):
        return None

    monkeypatch.setattr(
        "aistudio_api.api.routes_accounts.probe_account_email", fake_email
    )

    session = MagicMock()
    session.page = MagicMock()
    session._auth_file = "data/accounts/some_other_account/auth.json"
    session.extract_account_email = MagicMock(return_value="{{EMAIL_t6dnimxs}}")
    session.switch_auth = MagicMock()

    original_service = runtime_state.account_service
    original_client = runtime_state.client
    runtime_state.account_service = service
    runtime_state.client = MagicMock(_session=session)
    try:
        async with _client() as client:
            resp = await client.post(f"/api/accounts/{acc.id}/detect-email")

        assert resp.status_code == 200
        # 当前页属于别的账号，绝不能把它的邮箱写进来
        session.extract_account_email.assert_not_called()
        assert resp.json()["email"] is None
    finally:
        runtime_state.account_service = original_service
        runtime_state.client = original_client

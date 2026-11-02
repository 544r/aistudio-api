"""Unit tests for account_orchestrator module."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_orchestrator import (
    ensure_active_account,
    record_rotator_event,
    try_switch_account,
)
from aistudio_api.application.account_rotator import AccountRotator, AccountStats
from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.account.account_store import AccountMeta, AccountStore
from aistudio_api.infrastructure.gateway.client import AIStudioClient


@pytest.mark.asyncio
async def test_try_switch_account_returns_false_when_uninitialized():
    # Save original state
    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service
    orig_client = runtime_state.client

    try:
        runtime_state.rotator = None
        assert await try_switch_account() is False
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service
        runtime_state.client = orig_client


@pytest.mark.asyncio
async def test_try_switch_account_double_check_reuse():
    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)
    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.session = MagicMock()

    # Active account is now "acc_2"
    active_acc = AccountMeta(
        id="acc_2",
        name="Account 2",
        email="acc2@gmail.com",
        created_at="2026-01-01",
    )
    mock_service.get_active_account = MagicMock(return_value=active_acc)

    # acc_2 is available for target model
    mock_rotator.is_account_available = MagicMock(return_value=True)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service
    orig_client = runtime_state.client

    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service
        runtime_state.client = mock_client

        # Call with failed_account_id = "acc_1", current is already "acc_2" and available
        reused = await try_switch_account(
            model="gemini-2.5-flash", failed_account_id="acc_1"
        )
        assert reused is True
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service
        runtime_state.client = orig_client


@pytest.mark.asyncio
async def test_ensure_active_account_early_switch():
    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)

    active_acc = AccountMeta(
        id="acc_1",
        name="Account 1",
        email="acc1@gmail.com",
        created_at="2026-01-01",
    )
    mock_service.get_active_account = MagicMock(return_value=active_acc)

    # acc_1 is NOT available for this model
    mock_rotator.is_account_available = MagicMock(return_value=False)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service

    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service

        # attempt > 0 does nothing
        await ensure_active_account(attempt=1, model="gemini-2.5-flash")

        # attempt == 0 with unavailable model calls try_switch_account
        mock_rotator.is_account_available.assert_not_called()
        await ensure_active_account(attempt=0, model="gemini-2.5-flash")
        mock_rotator.is_account_available.assert_called_with(
            "acc_1", model="gemini-2.5-flash"
        )
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service


def test_record_rotator_event():
    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)
    active_acc = AccountMeta(
        id="acc_1",
        name="Account 1",
        email="acc1@gmail.com",
        created_at="2026-01-01",
    )
    mock_service.get_active_account = MagicMock(return_value=active_acc)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service

    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service

        record_rotator_event("success", model="gemini-2.5-flash")
        mock_rotator.record_success.assert_called_with(
            "acc_1", model="gemini-2.5-flash", usage=None
        )

        record_rotator_event("rate_limited", model="gemini-2.5-flash")
        mock_rotator.record_rate_limited.assert_called_with(
            "acc_1", model="gemini-2.5-flash"
        )

        record_rotator_event("auth_error", model="gemini-2.5-flash")
        mock_rotator.record_auth_error.assert_called_with(
            "acc_1", model="gemini-2.5-flash"
        )

        record_rotator_event("error", model="gemini-2.5-flash")
        mock_rotator.record_error.assert_called_with("acc_1", model="gemini-2.5-flash")
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service


@pytest.mark.asyncio
async def test_try_switch_account_single_account_recovery():
    """单账号发生 403 鉴权错误时，自动触发当前会话与 BotGuard 强制重建。"""
    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)
    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.session = MagicMock()
    mock_client.clear_templates = MagicMock()

    active_acc = AccountMeta(
        id="acc_1",
        name="Single Account",
        email="single@gmail.com",
        created_at="2026-01-01",
    )
    mock_service.get_active_account = MagicMock(return_value=active_acc)
    mock_service.activate_account = MagicMock(return_value=active_acc)
    # 模拟 rotator 只有唯一账号 acc_1
    from unittest.mock import AsyncMock

    mock_service.activate_account = AsyncMock(return_value=active_acc)
    mock_rotator.get_next_account = AsyncMock(return_value=active_acc)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service
    orig_client = runtime_state.client

    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service
        runtime_state.client = mock_client

        recovered = await try_switch_account(
            model="gemini-3.8-flash",
            failed_account_id="acc_1",
        )
        assert recovered is True
        mock_client.clear_templates.assert_called_once()
        mock_service.activate_account.assert_called_once_with(
            "acc_1",
            mock_client.session,
        )
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service
        runtime_state.client = orig_client


@pytest.mark.asyncio
async def test_try_switch_account_401_repairs_session_in_place_before_giving_up():
    """无处可切时的首次 401：先原地重导航自救（吃下 Google 的新一代滚动 Cookie）。"""
    import time
    from unittest.mock import AsyncMock

    from aistudio_api.application import account_orchestrator as orchestrator

    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)
    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.session = MagicMock()

    active_acc = AccountMeta(
        id="acc_1",
        name="Single Account",
        email="single@gmail.com",
        created_at="2026-01-01",
    )
    mock_service.get_active_account = MagicMock(return_value=active_acc)
    mock_service.activate_account = AsyncMock(return_value=active_acc)
    # 无备用账号：单账号模式下 401 只能自救
    mock_rotator.get_next_account = AsyncMock(return_value=None)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service
    orig_client = runtime_state.client
    orig_selfheal = orchestrator._selfheal_at
    orchestrator._selfheal_at = {}

    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service
        runtime_state.client = mock_client

        result = await try_switch_account(
            model="gemini-3.8-flash",
            failed_account_id="acc_1",
            is_session_expired=True,
        )
        assert result is True
        mock_service.activate_account.assert_called_once_with(
            "acc_1", mock_client.session
        )
        mock_rotator.record_session_expired.assert_not_called()
        # 自愈已记账：同一账号在冷却窗口内不会反复重导航
        assert orchestrator._selfheal_at["acc_1"] <= time.time()
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service
        runtime_state.client = orig_client
        orchestrator._selfheal_at = orig_selfheal


@pytest.mark.asyncio
async def test_try_switch_account_session_expired_strictly_forbids_in_place_retry():
    """自愈刚做过、仍然 401：确认救不回来，判登录态失效并严禁在原账号重试。"""
    import time
    from unittest.mock import AsyncMock

    from aistudio_api.application import account_orchestrator as orchestrator

    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)
    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.session = MagicMock()

    active_acc = AccountMeta(
        id="acc_1",
        name="Single Account",
        email="single@gmail.com",
        created_at="2026-01-01",
    )
    mock_service.get_active_account = MagicMock(return_value=active_acc)
    mock_service.activate_account = AsyncMock()
    # 当 is_session_expired=True 且无其他备用账号时，rotator 返回 None
    mock_rotator.get_next_account = AsyncMock(return_value=None)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service
    orig_client = runtime_state.client
    orig_selfheal = orchestrator._selfheal_at
    # 冷却窗口内已经原地自救过一次
    orchestrator._selfheal_at = {"acc_1": time.time()}

    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service
        runtime_state.client = mock_client

        result = await try_switch_account(
            model="gemini-3.8-flash",
            failed_account_id="acc_1",
            is_session_expired=True,
        )
        # 严禁原地重试，必须返回 False
        assert result is False
        mock_rotator.record_session_expired.assert_called_once_with(
            "acc_1", model="gemini-3.8-flash"
        )
        mock_service.activate_account.assert_not_called()
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service
        runtime_state.client = orig_client
        orchestrator._selfheal_at = orig_selfheal


@pytest.mark.asyncio
async def test_try_switch_account_session_expired_switches_to_healthy_account():
    """同 Cookie 或备用账号中某一个失效时，仅该账号失效并平滑切换到同 Cookie 的另一个正常子账号。"""
    from unittest.mock import AsyncMock

    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)
    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.session = MagicMock()

    acc1 = AccountMeta(
        id="acc_1", name="Cookie A u/0", email="user@gmail.com", created_at="2026-01-01"
    )
    acc2 = AccountMeta(
        id="acc_2", name="Cookie A u/1", email="user@gmail.com", created_at="2026-01-01"
    )

    mock_service.get_active_account = MagicMock(return_value=acc1)
    mock_service.activate_account = AsyncMock(return_value=acc2)
    # 成功挑中同 Cookie 的备用子账号 acc_2
    mock_rotator.get_next_account = AsyncMock(return_value=acc2)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service
    orig_client = runtime_state.client

    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service
        runtime_state.client = mock_client

        result = await try_switch_account(
            model="gemini-3.8-flash",
            failed_account_id="acc_1",
            is_session_expired=True,
        )
        assert result is True
        # 有备用账号时不立刻拉黑 401 账号：只记短冷却，登录态交给后台探活仲裁
        # （同批导入的账号经常一起到龄，误拉黑 24 小时会级联清空账号池）
        mock_rotator.record_session_expired.assert_not_called()
        mock_rotator.record_auth_error.assert_called_once_with(
            "acc_1", model="gemini-3.8-flash", cooldown_seconds=300.0
        )
        # 成功激活 acc_2
        mock_service.activate_account.assert_called_once_with(
            "acc_2",
            mock_client.session,
        )
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service
        runtime_state.client = orig_client


def test_account_stats_session_expired_permanent_isolation():
    """账号标记为 session_expired 后处于永久禁用状态，美西跨天不自动重置，ignore_auth_cooldown 也不可用。"""
    stats = AccountStats(account_id="acc_test")
    assert stats.is_available("gemini-3.8-flash")

    stats.record_session_expired(model="gemini-3.8-flash")
    assert stats.session_expired is True
    assert not stats.is_available("gemini-3.8-flash")
    # 即使 ignore_auth_cooldown 也绝对不可用
    assert not stats.is_available("gemini-3.8-flash", ignore_auth_cooldown=True)

    # 模拟跨天
    stats._check_date_reset("2099-01-01")
    assert stats.session_expired is True

    # 仅手动清除冷却/重新导入时恢复
    stats.clear_cooldown()
    assert stats.session_expired is False
    assert stats.is_available("gemini-3.8-flash")


def test_account_stats_short_cooldown_sync():
    """Verify short 60s cooldown is tracked accurately for monitoring."""
    stats = AccountStats(account_id="acc_test")
    stats.record_rate_limited(model="gemini-2.5-flash")

    assert not stats.is_available("gemini-2.5-flash")
    rem = stats.get_cooldown_remaining("gemini-2.5-flash")
    assert 0 < rem <= 60.0

    stats.record_rate_limited(model="gemini-2.5-flash")
    assert not stats.is_available("gemini-2.5-flash")
    rem2 = stats.get_cooldown_remaining("gemini-2.5-flash")
    assert 0 < rem2 <= 60.0

    stats.record_rate_limited(model="gemini-2.5-flash")
    assert not stats.is_available("gemini-2.5-flash")
    rem3 = stats.get_cooldown_remaining("gemini-2.5-flash")
    assert rem3 > 59.0



def test_safe_account_dir_path_traversal_rejection(tmp_path):
    """Verify _safe_account_dir rejects path traversal and malicious account IDs."""
    from aistudio_api.infrastructure.account.account_store import _safe_account_dir

    base = tmp_path / "accounts"
    base.mkdir()

    # Valid account IDs
    valid_dir = _safe_account_dir(base, "user_123")
    assert valid_dir == (base / "user_123").resolve()

    valid_email = _safe_account_dir(base, "test.user@gmail.com")
    assert valid_email == (base / "test.user@gmail.com").resolve()

    # Invalid / traversal IDs
    for bad_id in [
        "../etc/passwd",
        "..",
        "user/subdir",
        "user\\subdir",
        "foo/../bar",
        "",
    ]:
        with pytest.raises(ValueError):
            _safe_account_dir(base, bad_id)


def test_cleanup_ghost_accounts():
    """测试幽灵账号清理：已被删除但残留在 rotator_state 中的失效/429 账号被自动检测并彻底剔除。"""
    store = MagicMock(spec=AccountStore)
    real_acc = AccountMeta(
        id="real_acc", name="Real", email="real@gmail.com", created_at="2026-01-01"
    )
    store.list_accounts.return_value = [real_acc]

    rotator = AccountRotator(account_store=store)
    # 使用公共 API 模拟持久化恢复或历史遗留的失效/429 幽灵账号
    rotator.add_account("ghost_acc_1")
    rotator.record_rate_limited("ghost_acc_1")
    rotator.add_account("ghost_acc_2")
    rotator.record_session_expired("ghost_acc_2")

    cleaned = rotator.cleanup_ghost_accounts()
    assert set(cleaned) == {"ghost_acc_1", "ghost_acc_2"}
    assert "ghost_acc_1" not in rotator.get_all_stats()
    assert "ghost_acc_2" not in rotator.get_all_stats()
    assert "real_acc" in rotator.get_all_stats()

    # 测试单账号删除同步
    rotator.remove_account("real_acc")
    assert "real_acc" not in rotator._stats


@pytest.mark.asyncio
async def test_newly_imported_account_manual_activation_stays_sticky_on_first_request(
    tmp_path,
):
    """Regression for Issue #7:
    When a new account is imported while the server is running and manually activated,
    the first generateContent request must NOT switch back to the old account due to missing rotator stats.
    """
    from unittest.mock import AsyncMock

    store = AccountStore(accounts_dir=tmp_path / "accounts_issue7")
    store.save_account(
        name="Old Account",
        email="old@gmail.com",
        storage_state={"cookies": [{"name": "SID", "value": "old"}]},
    )
    # Initialize rotator with only old_acc existing at startup
    rotator = AccountRotator(account_store=store)
    service = AccountService(store)

    # Import new_acc at runtime after rotator was already initialized
    new_acc = service.save_account_from_cookies(
        name="New Account",
        email="new@gmail.com",
        storage_state={"cookies": [{"name": "SID", "value": "new"}]},
    )
    service.set_active_account(new_acc.id)

    # Even before explicit rotator.add_account(), rotator must recognize new_acc from store as available
    assert rotator.is_account_available(new_acc.id, model="gemini-3.8-flash") is True
    assert rotator.get_account_stats(new_acc.id) is not None

    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.session = MagicMock()
    service.activate_account = AsyncMock(return_value=new_acc)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service
    orig_client = runtime_state.client
    try:
        runtime_state.rotator = rotator
        runtime_state.account_service = service
        runtime_state.client = mock_client

        await ensure_active_account(attempt=0, model="models/gemini-3.8-flash")

        # Active account must remain new_acc and never fail over to old_acc
        active = service.get_active_account()
        assert active is not None
        assert active.id == new_acc.id
        service.activate_account.assert_not_awaited()
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service
        runtime_state.client = orig_client


@pytest.mark.asyncio
async def test_401_rescue_prefers_rotate_cookies_over_browser_repair(monkeypatch):
    """401 抢救链：纯 HTTP RotateCookies 成功时不再走浏览器重导航（省几秒）。"""
    from unittest.mock import AsyncMock

    from aistudio_api.application import account_orchestrator as orchestrator

    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)
    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.session = MagicMock()

    active_acc = AccountMeta(
        id="acc_1", name="A", email="single@gmail.com", created_at="2026-01-01"
    )
    mock_service.get_active_account = MagicMock(return_value=active_acc)
    mock_service.activate_account = AsyncMock(return_value=active_acc)
    # 无备用账号：触发抢救链
    mock_rotator.get_next_account = AsyncMock(return_value=None)
    mock_rotator.is_account_available = MagicMock(return_value=True)

    rotate_calls: list[str] = []
    repair_calls: list[str] = []

    async def fake_rotate(auth_path):
        rotate_calls.append(str(auth_path))
        return ["__Secure-1PSIDTS"]

    async def fake_repair(account_id, model=None):
        repair_calls.append(account_id)
        return True

    monkeypatch.setattr(orchestrator, "_try_rotate_cookies_repair", fake_rotate)
    monkeypatch.setattr(orchestrator, "_try_in_place_session_repair", fake_repair)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service
    orig_client = runtime_state.client
    orig_selfheal = orchestrator._selfheal_at
    orchestrator._selfheal_at = {}
    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service
        runtime_state.client = mock_client

        result = await try_switch_account(
            model="gemini-3.8-flash",
            failed_account_id="acc_1",
            is_session_expired=True,
        )
        assert result is True
        assert len(rotate_calls) == 1
        # RotateCookies 成功 → 浏览器重导航一次都不该发生
        assert repair_calls == []
        mock_rotator.record_session_expired.assert_not_called()
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service
        runtime_state.client = orig_client
        orchestrator._selfheal_at = orig_selfheal


@pytest.mark.asyncio
async def test_switch_target_login_redirect_marks_expired():
    """切换目标被 Google 重定向到登录页：必须记账标记失效，不能把异常裸抛出去。

    否则账号会一直显示“就绪/当前激活”，每次被选中都让请求失败一次，而前端
    永远看不到任何失效状态——这正是用户反馈的“报错但不自动禁用”。
    """
    from unittest.mock import AsyncMock

    from aistudio_api.domain.errors import SessionExpiredError

    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)
    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.session = MagicMock()

    acc1 = AccountMeta(
        id="acc_1", name="A", email="single@gmail.com", created_at="2026-01-01"
    )
    acc2 = AccountMeta(
        id="acc_2", name="B", email="{{EMAIL_3dnmwpqr}}", created_at="2026-01-01"
    )
    mock_service.get_active_account = MagicMock(return_value=acc1)
    mock_service.activate_account = AsyncMock(
        side_effect=SessionExpiredError("Cookie 认证失败，重新注入并导航后仍处于登录页")
    )
    mock_rotator.get_next_account = AsyncMock(return_value=acc2)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service
    orig_client = runtime_state.client
    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service
        runtime_state.client = mock_client

        result = await try_switch_account(
            model="gemini-3.8-flash",
            failed_account_id="acc_1",
            is_session_expired=True,
        )
        assert result is False
        # 切换目标自己撞了登录页：必须被标记为登录态失效
        mock_rotator.record_session_expired.assert_called_once_with(
            "acc_2", model="gemini-3.8-flash"
        )
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service
        runtime_state.client = orig_client


@pytest.mark.asyncio
async def test_rotate_cookies_rescue_reloads_active_browser(monkeypatch):
    """RotateCookies 成功后必须把新代次灌回当前浏览器，并清掉失效标记。

    只改 auth.json 不重载页面时，重试仍会拿旧 Cookie 再撞一次 401，抢救等于没做。
    """
    from unittest.mock import AsyncMock

    from aistudio_api.application import (
        account_health,
        account_orchestrator as orchestrator,
    )

    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)
    mock_client = MagicMock(spec=AIStudioClient)
    mock_session = MagicMock()
    mock_session.is_ready = True
    mock_session.reload_current_account_cookies = AsyncMock(return_value=True)
    mock_client.session = mock_session

    active = AccountMeta(
        id="acc_1", name="A", email="a@example.com", created_at="2026-01-01"
    )
    mock_service.get_account_auth_path = MagicMock(return_value="C:/tmp/auth.json")
    mock_service.get_active_account = MagicMock(return_value=active)

    async def fake_rotate(_path):
        return ["SIDCC"]

    monkeypatch.setattr(account_health, "_rotate_rescue_cooldown_ok", lambda _id: True)
    monkeypatch.setattr(account_health, "rotate_account_cookies", fake_rotate)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service
    orig_client = runtime_state.client
    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service
        runtime_state.client = mock_client

        assert await orchestrator._try_rotate_cookies_repair("acc_1") is True
        mock_rotator.set_session_expired.assert_called_once_with("acc_1", False)
        mock_session.reload_current_account_cookies.assert_awaited_once_with(
            include_rolling=True
        )
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service
        runtime_state.client = orig_client


@pytest.mark.asyncio
async def test_rotate_cookies_rescue_falls_through_when_reload_fails(monkeypatch):
    """新代次灌不回浏览器时不能假装成功，要让后续重导航/切号继续接管。"""
    from unittest.mock import AsyncMock

    from aistudio_api.application import (
        account_health,
        account_orchestrator as orchestrator,
    )

    mock_rotator = MagicMock(spec=AccountRotator)
    mock_service = MagicMock(spec=AccountService)
    mock_client = MagicMock(spec=AIStudioClient)
    mock_session = MagicMock()
    mock_session.is_ready = True
    mock_session.reload_current_account_cookies = AsyncMock(return_value=False)
    mock_client.session = mock_session

    active = AccountMeta(
        id="acc_1", name="A", email="a@example.com", created_at="2026-01-01"
    )
    mock_service.get_account_auth_path = MagicMock(return_value="C:/tmp/auth.json")
    mock_service.get_active_account = MagicMock(return_value=active)

    async def fake_rotate(_path):
        return ["SIDCC"]

    monkeypatch.setattr(account_health, "_rotate_rescue_cooldown_ok", lambda _id: True)
    monkeypatch.setattr(account_health, "rotate_account_cookies", fake_rotate)

    orig_rotator = runtime_state.rotator
    orig_service = runtime_state.account_service
    orig_client = runtime_state.client
    try:
        runtime_state.rotator = mock_rotator
        runtime_state.account_service = mock_service
        runtime_state.client = mock_client

        assert await orchestrator._try_rotate_cookies_repair("acc_1") is False
        mock_rotator.set_session_expired.assert_called_once_with("acc_1", False)
    finally:
        runtime_state.rotator = orig_rotator
        runtime_state.account_service = orig_service
        runtime_state.client = orig_client

"""Tests for system and configuration routes."""

from __future__ import annotations

import httpx
import pytest

from aistudio_api.api.app import app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_health_endpoint():
    async with _client() as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_auth_check_endpoint():
    async with _client() as client:
        resp = await client.get("/auth/check")
        assert resp.status_code == 200
        data = resp.json()
        assert "auth_enabled" in data


@pytest.mark.asyncio
async def test_userscript_download_endpoint():
    async with _client() as client:
        resp = await client.get("/userscripts/aistudio-cookie-push.user.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers.get("content-type", "")
        assert "==UserScript==" in resp.text

        # 浏览器直接打开（Accept: text/html）也要返回脚本本身，不能被 SPA 中间件改写
        nav = await client.get(
            "/userscripts/aistudio-cookie-push.user.js",
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        assert nav.status_code == 200
        assert "==UserScript==" in nav.text


@pytest.mark.asyncio
async def test_spa_endpoints():
    import asyncio

    async with _client() as client:
        responses = await asyncio.gather(
            *[
                client.get(path, headers={"Accept": "text/html"})
                for path in ("/", "/login", "/accounts", "/settings")
            ]
        )
        for resp in responses:
            assert resp.status_code == 200
            assert "<!DOCTYPE html>" in resp.text
            assert "Accept" in resp.headers.get("Vary", "")
            assert "no-store" in resp.headers.get("Cache-Control", "")


@pytest.mark.asyncio
async def test_get_config_endpoint():
    async with _client() as client:
        resp = await client.get("/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "port" in data
        assert "browser_port" in data
        assert "yaml_content" in data


@pytest.mark.asyncio
async def test_update_config_yaml_validation():
    async with _client() as client:
        resp = await client.put(
            "/config/yaml", json={"yaml_content": "invalid: [yaml: broken"}
        )
        assert resp.status_code == 400

        resp_non_dict = await client.put(
            "/config/yaml", json={"yaml_content": "- item1\n- item2"}
        )
        assert resp_non_dict.status_code == 400


@pytest.mark.asyncio
async def test_rotation_status_and_clear_cooldown():
    from unittest.mock import MagicMock

    from aistudio_api.api.state import runtime_state
    from aistudio_api.application.account_rotator import AccountRotator

    mock_rotator = MagicMock(spec=AccountRotator)
    mock_rotator.get_all_stats.return_value = {"acc_1": {"requests": 1}}

    runtime_state.rotator = mock_rotator
    async with _client() as client:
        resp = await client.get("/rotation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "sticky"
        assert "accounts" in data

        resp_clear = await client.post(
            "/rotation/clear-cooldown", json={"account_id": "acc_1"}
        )
        assert resp_clear.status_code == 200
        mock_rotator.clear_cooldown.assert_called_once_with("acc_1", model=None)


@pytest.mark.asyncio
async def test_update_timeouts_config_applies_and_persists(tmp_path, monkeypatch):
    """PUT /config/timeouts 立即生效并写入 config.yaml 的 timeouts 段。"""
    import yaml

    from aistudio_api.config import settings

    config_path = tmp_path / "config.yaml"
    config_path.write_text("models: {}\n", encoding="utf-8")
    monkeypatch.setenv("AISTUDIO_CONFIG_FILE", str(config_path))
    original_stream = settings.timeout_stream
    original_replay = settings.timeout_replay
    try:
        async with _client() as client:
            resp = await client.put(
                "/config/timeouts", json={"stream": 1800, "replay": 900}
            )
        assert resp.status_code == 200
        assert resp.json()["timeout_stream"] == 1800
        assert resp.json()["timeout_replay"] == 900
        assert settings.timeout_stream == 1800
        assert settings.timeout_replay == 900

        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert saved["timeouts"] == {"stream": 1800, "replay": 900}

        async with _client() as client:
            out_of_range = await client.put("/config/timeouts", json={"stream": 0})
            empty = await client.put("/config/timeouts", json={})
        assert out_of_range.status_code == 400
        assert empty.status_code == 400
    finally:
        settings.timeout_stream = original_stream
        settings.timeout_replay = original_replay


def test_apply_configured_timeouts_reads_yaml(tmp_path):
    """启动时从 config.yaml 读取超时覆盖并写入 settings。"""
    from aistudio_api.config import settings
    from aistudio_api.infrastructure.gateway.model_defaults import (
        apply_configured_timeouts,
    )

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "timeouts:\n  stream: 1200\n  replay: 300\n", encoding="utf-8"
    )
    original_stream = settings.timeout_stream
    original_replay = settings.timeout_replay
    try:
        overrides = apply_configured_timeouts(config_path)
        assert overrides == {"stream": 1200, "replay": 300}
        assert settings.timeout_stream == 1200
        assert settings.timeout_replay == 300
    finally:
        settings.timeout_stream = original_stream
        settings.timeout_replay = original_replay


@pytest.mark.asyncio
async def test_update_timeouts_keeps_runtime_value_when_config_unwritable(monkeypatch):
    """配置文件只读时，超时仍在当前进程立即生效，并明确返回未持久化。"""
    from aistudio_api.config import settings

    def boom(**_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(
        "aistudio_api.infrastructure.gateway.model_defaults.update_configured_timeouts",
        boom,
    )
    original_stream = settings.timeout_stream
    try:
        async with _client() as client:
            resp = await client.put("/config/timeouts", json={"stream": 900})
        assert resp.status_code == 200
        data = resp.json()
        assert data["persisted"] is False
        assert data["timeout_stream"] == 900
        assert settings.timeout_stream == 900
    finally:
        settings.timeout_stream = original_stream

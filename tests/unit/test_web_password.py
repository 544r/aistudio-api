"""Tests for the runtime-updatable management password."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.config import settings
from aistudio_api.infrastructure.auth.web_password import (
    clear_web_password_cache,
    set_web_password,
    verify_web_password,
    web_auth_enabled,
)


@pytest.fixture(autouse=True)
def web_auth_file(monkeypatch, tmp_path) -> Iterator[Path]:
    path = tmp_path / "web_auth.json"
    monkeypatch.setenv("AISTUDIO_WEB_AUTH_FILE", str(path))
    clear_web_password_cache()
    yield path
    clear_web_password_cache()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def test_env_password_is_used_until_a_persisted_password_exists(monkeypatch):
    monkeypatch.setattr(settings, "web_password", "test-current-password")

    assert web_auth_enabled()
    assert verify_web_password("test-current-password")
    assert not verify_web_password("wrong-password")


def test_set_password_persists_hash_and_switches_immediately(
    monkeypatch, web_auth_file: Path
):
    monkeypatch.setattr(settings, "web_password", "test-current-password")
    set_web_password("test-new-password")

    assert not verify_web_password("test-current-password")
    assert verify_web_password("test-new-password")

    raw = web_auth_file.read_text(encoding="utf-8")
    assert "test-new-password" not in raw
    data = json.loads(raw)
    assert data["algorithm"] == "pbkdf2_sha256"

    clear_web_password_cache()
    assert verify_web_password("test-new-password")
    assert not verify_web_password("test-current-password")
    assert web_auth_enabled()


def test_persisted_password_keeps_auth_enabled_without_env(
    monkeypatch, web_auth_file: Path
):
    monkeypatch.setattr(settings, "web_password", None)
    web_auth_file.write_text(
        json.dumps(
            {
                "version": 1,
                "algorithm": "pbkdf2_sha256",
                "iterations": 210000,
                "salt": "not-valid-base64",
                "hash": "not-valid-base64",
            }
        ),
        encoding="utf-8",
    )

    assert web_auth_enabled()
    assert not verify_web_password("anything")


@pytest.mark.asyncio
async def test_change_password_endpoint_applies_without_restart(monkeypatch):
    monkeypatch.setattr(settings, "web_password", "test-current-password")

    async with _client() as client:
        response = await client.put(
            "/auth/password",
            headers={"Authorization": "Bearer test-current-password"},
            json={
                "current_password": "test-current-password",
                "new_password": "test-new-password",
            },
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True

        old = await client.get(
            "/stats", headers={"Authorization": "Bearer test-current-password"}
        )
        assert old.status_code == 401

        new = await client.get(
            "/stats", headers={"Authorization": "Bearer test-new-password"}
        )
        assert new.status_code == 200

        check = await client.get("/auth/check")
        assert check.json()["auth_enabled"] is True


@pytest.mark.asyncio
async def test_change_password_endpoint_rejects_wrong_current(monkeypatch):
    monkeypatch.setattr(settings, "web_password", "test-current-password")

    async with _client() as client:
        response = await client.put(
            "/auth/password",
            headers={"Authorization": "Bearer test-current-password"},
            json={
                "current_password": "wrong-password",
                "new_password": "test-new-password",
            },
        )
        assert response.status_code == 403
        assert verify_web_password("test-current-password")


@pytest.mark.asyncio
async def test_change_password_endpoint_rejected_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(settings, "web_password", None)

    async with _client() as client:
        response = await client.put(
            "/auth/password",
            json={
                "current_password": "whatever",
                "new_password": "test-new-password",
            },
        )
        assert response.status_code == 400

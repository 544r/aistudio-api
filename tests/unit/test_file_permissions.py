"""敏感 JSON 落盘权限：auth.json / 运行配置只能是属主可读写。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from aistudio_api.infrastructure.account.account_store import _atomic_write_json
from aistudio_api.infrastructure.utils.common import atomic_write_json

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX 权限位在 Windows 上不可用"
)


@pytest.mark.parametrize("writer", [_atomic_write_json, atomic_write_json])
def test_sensitive_json_is_owner_only(tmp_path: Path, writer) -> None:
    target = tmp_path / "auth.json"
    writer(target, {"cookies": [{"name": "SID", "value": "secret"}]})

    assert stat.S_IMODE(target.stat().st_mode) == 0o600

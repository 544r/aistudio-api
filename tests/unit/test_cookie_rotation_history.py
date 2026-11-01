"""CookieRotationHistory 单元测试。"""

from __future__ import annotations

from aistudio_api.application.cookie_rotation_history import (
    CookieRotationHistory,
    diff_cookie_values_as_changes,
    short_digest,
    snapshot_cookie_values,
)


def test_record_emits_event_with_changes():
    history = CookieRotationHistory(capacity=10)
    event = history.record(
        account_id="acc_1",
        source="probe",
        kind="rotated",
        reason="探活响应 Set-Cookie 回写",
        cookie_count=16,
        fingerprint="abc12345",
        changes=diff_cookie_values_as_changes(
            {"__Secure-1PSIDTS": "gen-1"}, {"__Secure-1PSIDTS": "gen-2"}
        ),
    )
    assert event is not None
    assert event.source == "probe"
    assert event.kind == "rotated"
    assert len(event.changes) == 1
    change = event.changes[0]
    assert change.name == "__Secure-1PSIDTS"
    assert change.before == short_digest("gen-1")
    assert change.after == short_digest("gen-2")
    # 事件里绝不能出现 Cookie 明文
    assert "gen-1" not in str(event.__dict__)
    assert "gen-2" not in str(event.__dict__)


def test_record_skips_when_fingerprint_unchanged_and_no_changes():
    history = CookieRotationHistory(capacity=10)
    history.note_fingerprint("acc_1", "fp-1")
    event = history.record(
        account_id="acc_1",
        source="save",
        kind="rotated",
        fingerprint="fp-1",
        changes=[],
    )
    assert event is None
    assert history.recent() == []


def test_recent_returns_newest_first_and_bounded():
    history = CookieRotationHistory(capacity=3)
    for i in range(5):
        history.record(
            account_id=f"acc_{i}",
            source="save",
            kind="rotated",
            fingerprint=f"fp-{i}",
            changes=diff_cookie_values_as_changes({}, {"SID": f"v{i}"}),
        )
    events = history.recent(limit=10)
    assert len(events) == 3  # 容量上限
    assert events[0].account_id == "acc_4"  # 最新在前
    assert [e.seq for e in events] == sorted((e.seq for e in events), reverse=True)


def test_previous_fingerprint_chaining():
    history = CookieRotationHistory(capacity=10)
    history.record(
        account_id="acc_1", source="save", kind="rotated", fingerprint="fp-a"
    )
    second = history.record(
        account_id="acc_1", source="probe", kind="rotated", fingerprint="fp-b"
    )
    assert second is not None
    assert second.previous_fingerprint == "fp-a"


def test_snapshot_and_diff_helpers():
    cookies = [{"name": "SID", "value": "v1"}, {"name": "SAPISID", "value": "s1"}]
    snap = snapshot_cookie_values(cookies)
    assert snap == {"SID": "v1", "SAPISID": "s1"}
    changes = diff_cookie_values_as_changes(snap, {"SID": "v1", "SAPISID": "s2"})
    assert len(changes) == 1
    assert changes[0].name == "SAPISID"

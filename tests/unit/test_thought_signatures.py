"""Tests for the in-process thought-signature store."""

from __future__ import annotations

from aistudio_api.application import thought_signatures


def setup_function() -> None:
    thought_signatures.reset()


def test_round_trip_uses_canonical_arguments() -> None:
    thought_signatures.remember(
        [
            {
                "id": "call_1",
                "name": "lookup",
                "args": {"b": 2, "a": 1},
                "thought_signature": "sig",
            }
        ]
    )

    assert thought_signatures.lookup("call_1", "lookup", {"a": 1, "b": 2}) == "sig"
    assert thought_signatures.lookup("call_1", "lookup", {"a": 1, "b": 3}) == ""
    assert thought_signatures.lookup("call_1", "other", {"a": 1, "b": 2}) == ""


def test_argument_json_string_matches_object() -> None:
    thought_signatures.remember(
        [
            {
                "id": "call_1",
                "name": "lookup",
                "arguments": '{"q":"x"}',
                "thought_signature": "sig",
            }
        ]
    )

    assert thought_signatures.lookup("call_1", "lookup", {"q": "x"}) == "sig"


def test_missing_id_or_signature_is_ignored() -> None:
    thought_signatures.remember(
        [
            {"id": "", "name": "lookup", "args": {}, "thought_signature": "sig"},
            {"id": "call_1", "name": "lookup", "args": {}},
        ]
    )

    assert thought_signatures.lookup("call_1", "lookup", {}) == ""


def test_capacity_evicts_oldest_entries() -> None:
    for index in range(4100):
        thought_signatures.remember(
            [
                {
                    "id": f"call_{index}",
                    "name": "lookup",
                    "args": {"index": index},
                    "thought_signature": f"sig_{index}",
                }
            ]
        )

    assert thought_signatures.lookup("call_0", "lookup", {"index": 0}) == ""
    assert thought_signatures.lookup("call_4099", "lookup", {"index": 4099}) == "sig_4099"

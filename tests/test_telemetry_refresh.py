"""遥测刷新事务契约：失败保留旧快照、完整后发布、坏缓存必须修复。"""
import json

import pytest

from tools.sentry.lib import telemetry_index as telemetry


def row(short="PYTHON-1", seen="2026-11-18T05:00:00Z"):
    return {"shortId": short, "status": "unresolved", "lastSeen": seen}


def detail(short="PYTHON-1", title="Maa task failed: Gift"):
    return {"shortId": short, "title": title, "status": "unresolved", "lastSeen": "2026-11-18T05:00:00Z", "event": {"id": "abcdef1234567890", "dateCreated": "2026-11-18T05:00:00Z", "release": {"version": "MaaAshArms@v1.0.0-beta.86"}}}


@pytest.fixture
def local_store(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "store_dir", lambda: tmp_path)
    telemetry.write_card(telemetry.IssueCard("PYTHON-OLD", title="old"))
    telemetry.save_index({"schema": telemetry.SCHEMA, "issues": {"PYTHON-OLD": {"lastSeen": "old"}}, "refreshed_at": "2000-01-01T00:00:00+00:00"})
    return tmp_path


def test_detail_failure_does_not_publish_or_delete_old(local_store, monkeypatch):
    baseline = {path.name: path.read_bytes() for path in local_store.glob("*.json")}
    monkeypatch.setattr(telemetry, "list_issues", lambda *a, **k: [row(), row("PYTHON-2")])
    def view(sentry, short, **kwargs):
        if short == "PYTHON-2":
            raise RuntimeError("network timeout")
        return detail(short)
    monkeypatch.setattr(telemetry, "issue_view", view)
    with pytest.raises(RuntimeError):
        telemetry.refresh("sentry", "target", days=36500)
    assert {path.name: path.read_bytes() for path in local_store.glob("*.json")} == baseline
    assert telemetry.read_card("PYTHON-OLD").title == "old"


def test_complete_refresh_publishes_consistent_cards(local_store, monkeypatch):
    monkeypatch.setattr(telemetry, "list_issues", lambda *a, **k: [row()])
    monkeypatch.setattr(telemetry, "issue_view", lambda *a, **k: detail())
    index, _ = telemetry.refresh("sentry", "target", days=36500)
    assert set(index["issues"]) == {"PYTHON-1"}
    assert telemetry.load_index() == index
    cards = telemetry.load_cards(index)
    assert [card.short_id for card in cards] == ["PYTHON-1"]
    assert cards[0].title == "Maa task failed: Gift"


def test_malformed_list_is_error_not_empty_success(monkeypatch):
    monkeypatch.setattr(telemetry.report_common, "run_sentry_json_value", lambda *a, **k: {"error": "bad response"})
    with pytest.raises(RuntimeError):
        telemetry.list_issues("sentry", "target", period="30d", limit=200, verbose=False, timeout=1)


def test_transient_list_failure_retries_then_succeeds(monkeypatch):
    calls = []
    def fetch(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("network timeout")
        return [row()]
    monkeypatch.setattr(telemetry.report_common, "run_sentry_json_value", fetch)
    assert telemetry.list_issues("sentry", "target", period="30d", limit=200, verbose=False, timeout=1) == [row()]
    assert len(calls) == 3


def test_persistent_failure_is_bounded(monkeypatch):
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("network timeout")
    monkeypatch.setattr(telemetry.report_common, "run_sentry_json_value", fail)
    with pytest.raises(RuntimeError):
        telemetry.issue_view("sentry", "PYTHON-1", verbose=False, timeout=1)
    assert len(calls) == 3


def test_truncated_list_cannot_be_marked_complete(local_store, monkeypatch):
    before = telemetry.index_path().read_bytes()
    monkeypatch.setattr(telemetry, "list_issues", lambda *a, **k: [row()])
    monkeypatch.setattr(telemetry, "issue_view", lambda *a, **k: detail())
    with pytest.raises(RuntimeError):
        telemetry.refresh("sentry", "target", limit=1, days=36500)
    assert telemetry.index_path().read_bytes() == before


def test_unreadable_card_is_refetched_even_if_last_seen_unchanged(local_store, monkeypatch):
    telemetry.save_index({"schema": telemetry.SCHEMA, "issues": {"PYTHON-1": {"lastSeen": row()["lastSeen"]}}})
    telemetry.card_path("PYTHON-1").write_text("{bad json", encoding="utf-8")
    calls = []
    monkeypatch.setattr(telemetry, "list_issues", lambda *a, **k: [row()])
    monkeypatch.setattr(telemetry, "issue_view", lambda *a, **k: calls.append(1) or detail())
    index, _ = telemetry.refresh("sentry", "target", days=36500)
    assert calls == [1] and telemetry.load_cards(index)[0].title



def test_readers_keep_consistent_generation(local_store, monkeypatch):
    monkeypatch.setattr(telemetry, "list_issues", lambda *a, **k: [row()])
    monkeypatch.setattr(telemetry, "issue_view", lambda *a, **k: detail(title="first"))
    first, _ = telemetry.refresh("sentry", "target", days=36500)
    monkeypatch.setattr(telemetry, "issue_view", lambda *a, **k: detail(title="second"))
    second, _ = telemetry.refresh("sentry", "target", days=36500, full_details=True)
    assert first["snapshot"] != second["snapshot"]
    assert telemetry.load_cards(first)[0].title == "first"
    assert telemetry.load_cards(second)[0].title == "second"


def test_force_refresh_requests_all_details(monkeypatch):
    calls = []
    monkeypatch.setattr(telemetry, "refresh_default", lambda **kwargs: calls.append(kwargs) or ({"issues": {}}, []))
    telemetry.load_or_refresh(force=True)
    assert calls[0]["full_details"] is True


def test_fresh_broken_cache_does_not_skip_refresh(local_store, monkeypatch):
    from datetime import datetime, timezone
    telemetry.save_index({"schema": telemetry.SCHEMA, "issues": {"PYTHON-BROKEN": {}}, "refreshed_at": datetime.now(timezone.utc).isoformat()})
    calls = []
    monkeypatch.setattr(telemetry, "refresh_default", lambda **kwargs: calls.append(kwargs) or ({"issues": {}}, []))
    telemetry.load_or_refresh(max_age=300)
    assert len(calls) == 1


def test_transport_failure_does_not_fallback_to_unfiltered_list(local_store, monkeypatch):
    queries = []
    def fail(*args, **kwargs):
        queries.append(kwargs.get("query"))
        raise RuntimeError("network timeout")
    monkeypatch.setattr(telemetry, "list_issues", fail)
    with pytest.raises(RuntimeError):
        telemetry.refresh("sentry", "target")
    assert queries == ["is:unresolved"]


def test_bad_detail_identity_is_retried_and_rejected(monkeypatch):
    calls = []
    monkeypatch.setattr(telemetry.report_common, "run_sentry_json_value", lambda *a, **k: calls.append(1) or detail("PYTHON-OTHER"))
    with pytest.raises(RuntimeError):
        telemetry.issue_view("sentry", "PYTHON-1", verbose=False, timeout=1)
    assert len(calls) == 3


def test_atomic_commit_failure_preserves_published_index(local_store, monkeypatch):
    before = telemetry.index_path().read_bytes()
    monkeypatch.setattr(telemetry, "list_issues", lambda *a, **k: [row()])
    monkeypatch.setattr(telemetry, "issue_view", lambda *a, **k: detail())
    def fail(*args, **kwargs):
        raise OSError("cannot publish")
    monkeypatch.setattr(telemetry, "save_index", fail)
    with pytest.raises((RuntimeError, OSError)):
        telemetry.refresh("sentry", "target", days=36500)
    assert telemetry.index_path().read_bytes() == before
    assert telemetry.read_card("PYTHON-OLD").title == "old"

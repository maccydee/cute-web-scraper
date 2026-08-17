import pytest

from cute_web_scraper.store import ResultStore, SnapshotStore


@pytest.fixture
def snapshots(tmp_path):
    return SnapshotStore(ResultStore(tmp_path / "r.db"))


def test_first_sighting_is_new(snapshots):
    change = snapshots.compare("https://a.com", "hello", seen_at="2026-01-01T00:00:00")
    assert change.status == "new"
    assert change.previous_seen_at is None
    assert change.diff == ""


def test_identical_content_is_same(snapshots):
    snapshots.compare("https://a.com", "hello", seen_at="2026-01-01T00:00:00")
    change = snapshots.compare("https://a.com", "hello", seen_at="2026-01-02T00:00:00")
    assert change.status == "same"
    assert change.previous_seen_at == "2026-01-01T00:00:00"
    assert change.added_lines == 0


def test_changed_content_reports_a_diff(snapshots):
    snapshots.compare("https://a.com", "price: £10\nstock: 5", seen_at="2026-01-01T00:00:00")
    change = snapshots.compare(
        "https://a.com", "price: £12\nstock: 5", seen_at="2026-01-02T00:00:00"
    )
    assert change.status == "changed"
    assert change.added_lines == 1
    assert change.removed_lines == 1
    assert "£12" in change.diff
    assert "£10" in change.diff


def test_the_snapshot_advances_after_each_check(snapshots):
    snapshots.compare("https://a.com", "v1", seen_at="2026-01-01T00:00:00")
    snapshots.compare("https://a.com", "v2", seen_at="2026-01-02T00:00:00")
    third = snapshots.compare("https://a.com", "v2", seen_at="2026-01-03T00:00:00")
    assert third.status == "same", "the second check should have become the new baseline"


def test_urls_are_tracked_independently(snapshots):
    snapshots.compare("https://a.com", "one", seen_at="2026-01-01T00:00:00")
    assert snapshots.compare("https://b.com", "two", seen_at="2026-01-01T00:00:00").status == "new"


def test_a_huge_diff_is_truncated(snapshots):
    snapshots.compare("https://a.com", "\n".join(str(i) for i in range(500)), seen_at="t1")
    change = snapshots.compare(
        "https://a.com", "\n".join(str(i * 7) for i in range(500)), seen_at="t2"
    )
    assert "diff truncated" in change.diff
    assert len(change.diff.splitlines()) < 250


def test_tracked_lists_what_is_being_watched(snapshots):
    snapshots.compare("https://a.com", "one", seen_at="2026-01-01T00:00:00")
    snapshots.compare("https://b.com", "two", seen_at="2026-01-02T00:00:00")
    tracked = snapshots.tracked()
    assert [t["url"] for t in tracked] == ["https://a.com", "https://b.com"]
    assert tracked[0]["seen_at"] == "2026-01-01T00:00:00"


def test_forget_removes_a_snapshot(snapshots):
    snapshots.compare("https://a.com", "one", seen_at="t")
    assert snapshots.forget("https://a.com") is True
    assert snapshots.tracked() == []
    assert snapshots.compare("https://a.com", "one", seen_at="t").status == "new"


def test_forgetting_an_unknown_url_is_false(snapshots):
    assert snapshots.forget("https://never-seen.com") is False


def test_snapshots_do_not_appear_as_a_result_table(tmp_path):
    store = ResultStore(tmp_path / "r.db")
    SnapshotStore(store).compare("https://a.com", "x", seen_at="t")
    store.save("real_data", [{"a": 1}])
    assert [t.name for t in store.list_tables()] == ["real_data"]

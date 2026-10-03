from concurrent.futures import ThreadPoolExecutor

import pytest

from clangy.store import Store, StoreError


def test_create_assigns_numbers_and_writes_frontmatter(tmp_path):
    store = Store(tmp_path)
    assert store.create("ticket", "Crash on load", "Body", {"thread": "https://t/1"}) == "T-0001"
    assert store.create("ticket", "Second", "Body", {}) == "T-0002"
    assert store.create("note", "A note", "Body", {}) == "N-0001"
    text = (tmp_path / "tickets" / "T-0001.md").read_text()
    assert text.startswith("---\nid: T-0001\ntitle: Crash on load\n")
    assert "status: new\n" in text and "thread: https://t/1\n" in text
    assert text.endswith("---\n\nBody\n")


def test_frontmatter_values_cannot_inject_lines(tmp_path):
    store = Store(tmp_path)
    store.create("plan", "Idea\nstatus: done", "Body", {})
    text = (tmp_path / "plans" / "P-0001.md").read_text()
    assert "title: Idea status: done\n" in text


@pytest.mark.parametrize(("title", "body"), [("", "body"), ("t", " "), ("t" * 121, "body"), ("t", "b" * 20001)])
def test_validation(tmp_path, title, body):
    with pytest.raises(StoreError):
        Store(tmp_path).create("ticket", title, body, {})


def test_concurrent_creates_get_unique_numbers(tmp_path):
    store = Store(tmp_path)
    with ThreadPoolExecutor(8) as pool:
        ids = list(pool.map(lambda i: store.create("ticket", f"T{i}", "Body", {}), range(40)))
    assert sorted(ids) == [f"T-{i:04d}" for i in range(1, 41)]


def test_add_report(tmp_path):
    store = Store(tmp_path)
    store.create("ticket", "Crash", "First", {})
    store.add_report("T-0001", "Same crash here", {"thread": "https://t/2"})
    text = (tmp_path / "tickets" / "T-0001.md").read_text()
    assert "## Report" in text and "- thread: https://t/2" in text and text.endswith("Same crash here\n")
    with pytest.raises(StoreError):
        store.add_report("T-0099", "x", {})
    with pytest.raises(StoreError):
        store.add_report("../../etc/passwd", "x", {})


def test_records(tmp_path):
    store = Store(tmp_path)
    store.append_record(5, "Request", "https://t/5", "**A**: one")
    store.append_record(5, "Request", "https://t/5", "**B**: two")
    text = (tmp_path / "records" / "5.md").read_text()
    assert text == "---\ntitle: Request\nthread: https://t/5\n---\n\n**A**: one\n\n**B**: two\n\n"

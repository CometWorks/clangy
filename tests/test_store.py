from concurrent.futures import ThreadPoolExecutor

import pytest

from clangy.store import Store, StoreError


def test_create_assigns_numbers_and_writes_frontmatter(tmp_path):
    store = Store(tmp_path, tmp_path / "records")
    assert store.create("ticket", "Crash on load", "Body", {"thread": "https://t/1"}) == "DIS-0001"
    assert store.create("ticket", "Second", "Body", {}) == "DIS-0002"
    assert store.create("note", "A note", "Body", {}) == "N-0001"
    text = (tmp_path / "tickets" / "DIS-0001.md").read_text()
    assert text.startswith("---\nid: DIS-0001\ntitle: Crash on load\n")
    assert "status: new\n" in text and "thread: https://t/1\n" in text
    assert text.endswith("---\n\n# Description\n\nBody\n\n# Work log\n")


def test_frontmatter_values_cannot_inject_lines(tmp_path):
    store = Store(tmp_path, tmp_path / "records")
    store.create("plan", "Idea\nstatus: done", "Body", {})
    text = (tmp_path / "plans" / "P-0001.md").read_text()
    assert "title: Idea status: done\n" in text


@pytest.mark.parametrize(("title", "body"), [("", "body"), ("t", " "), ("t" * 121, "body"), ("t", "b" * 20001)])
def test_validation(tmp_path, title, body):
    with pytest.raises(StoreError):
        Store(tmp_path, tmp_path / "records").create("ticket", title, body, {})


def test_concurrent_creates_get_unique_numbers(tmp_path):
    store = Store(tmp_path, tmp_path / "records")
    with ThreadPoolExecutor(8) as pool:
        ids = list(pool.map(lambda i: store.create("ticket", f"T{i}", "Body", {}), range(40)))
    assert sorted(ids) == [f"DIS-{i:04d}" for i in range(1, 41)]


def test_add_report_goes_above_the_work_log(tmp_path):
    store = Store(tmp_path, tmp_path / "records")
    store.create("ticket", "Crash", "First", {})
    (tmp_path / "tickets" / "DIS-0001.md").write_text((tmp_path / "tickets" / "DIS-0001.md").read_text() + "- Looked\n")
    store.add_report("DIS-0001", "Same crash here", {"thread": "https://t/2"})
    text = (tmp_path / "tickets" / "DIS-0001.md").read_text()
    assert "## Report" in text and "- thread: https://t/2" in text
    assert text.endswith("Same crash here\n\n# Work log\n- Looked\n")
    with pytest.raises(StoreError):
        store.add_report("DIS-0099", "x", {})
    with pytest.raises(StoreError):
        store.add_report("../../etc/passwd", "x", {})


def test_closed_tickets_keep_their_numbers(tmp_path):
    store = Store(tmp_path, tmp_path / "records")
    store.create("ticket", "Crash", "First", {})
    (tmp_path / "tickets" / "DIS-0001.md").rename(tmp_path / "tickets" / "completed" / "DIS-0001.md")
    with pytest.raises(StoreError, match="DIS-0001 is completed. If the problem came back"):
        store.add_report("DIS-0001", "Again", {})
    assert store.create("ticket", "Crash again", "Body", {}) == "DIS-0002"


def test_ticket_files_go_into_a_linked_note(tmp_path):
    store = Store(tmp_path, tmp_path / "records")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "info.log").write_text("one")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "info.log").write_text("two")
    (tmp_path / "b" / "NOTE.md").write_text("three")
    files = [(tmp_path / "a" / "info.log", "First"), (tmp_path / "b" / "info.log", "Second")]
    assert store.create("ticket", "Crash", "Body", {"thread": "https://t/1"}, files) == "DIS-0001"
    store.add_report("DIS-0001", "Again", {}, [(tmp_path / "b" / "NOTE.md", "Third")])
    ticket = (tmp_path / "tickets" / "DIS-0001.md").read_text()
    assert ticket.startswith("---\nid: DIS-0001\n") and "notes: N-0001, N-0002\n---\n" in ticket
    assert "- notes: N-0002\n" in ticket
    assert ticket.index("# Description") < ticket.index("## Report") < ticket.index("# Work log")
    note = tmp_path / "notes" / "N-0001"
    assert (note / "info.log").read_text() == "one" and (note / "info-2.log").read_text() == "two"
    text = (note / "NOTE.md").read_text()
    assert "ticket: DIS-0001\nthread: https://t/1\n" not in text and "ticket: DIS-0001\n" in text
    assert text.endswith("# Files\n\n- `info.log`: First\n- `info-2.log`: Second\n\n")
    assert (tmp_path / "notes" / "N-0002" / "NOTE-2.md").read_text() == "three"
    assert store.create("note", "Plain", "Body", {}) == "N-0003"
    assert (tmp_path / "notes" / "N-0003.md").exists()


def test_workspace_rules_are_written_once(tmp_path):
    Store(tmp_path, tmp_path / "records")
    assert (tmp_path / "AGENTS.md").read_text().startswith("# Clangy's workspace")
    (tmp_path / "tickets" / "Template.md").write_text("edited")
    Store(tmp_path, tmp_path / "records")
    assert (tmp_path / "tickets" / "Template.md").read_text() == "edited"


def test_records(tmp_path):
    store = Store(tmp_path, tmp_path / "records")
    store.append_record(5, "Request", "https://t/5", "**A**: one")
    store.append_record(5, "Request", "https://t/5", "**B**: two")
    text = (tmp_path / "records" / "5.md").read_text()
    assert text == "---\ntitle: Request\nthread: https://t/5\n---\n\n**A**: one\n\n**B**: two\n\n"


def test_tickets_continue_after_the_highest_dis_number(tmp_path):
    store = Store(tmp_path, tmp_path / "records")
    tickets = tmp_path / "tickets"
    for path in ("DIS-0010.md", "T-0042.md", "completed/DIS-0019.md", "canceled/DIS-0011.md"):
        (tickets / path).write_text("x")
    assert store.create("ticket", "Crash", "Body", {}) == "DIS-0020"
    assert (tickets / "DIS-0020.md").read_text().startswith("---\nid: DIS-0020\n")


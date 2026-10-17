import gzip

import pytest

from clangy.config import TasksConfig
from clangy.store import Store, StoreError, frontmatter
from clangy.tracker import Tracker, header_html, parse_header, to_text
from tests.fakes import FakeVikunja

THREAD = "https://discord.com/channels/1/1015"


@pytest.fixture
def vikunja():
    fake = FakeVikunja()
    yield fake
    fake.close()


@pytest.fixture
def tracker(vikunja, tmp_path):
    return Tracker(TasksConfig(url=vikunja.url, project=8, api_key="key"), tmp_path / "tickets", tmp_path / "notes")


def test_create_numbers_after_the_highest_ticket_and_keeps_files(tracker, vikunja, tmp_path):
    vikunja.add("DIS-0030: Old crash", done=True)
    vikunja.add("Not a ticket of Clangy's")
    log = tmp_path / "1015-info.log"
    log.write_bytes(b"log data")
    meta = {"thread": THREAD, "game": "se1"}
    doc_id = tracker.create(
        "ticket", "Crash\non load", "## Summary\n\nIt *crashes*.", meta, [(log, "Pulsar log (posted)")]
    )
    assert doc_id == "DIS-0031"
    task = vikunja.tasks[3]
    assert task["title"] == "DIS-0031: Crash on load"
    assert [l["title"] for l in task["labels"]] == ["se1"]
    assert vikunja.uploads == [(3, "1015-info.log", b"log data")]
    assert "<h2>Summary</h2>" in task["description"] and "<em>crashes</em>" in task["description"]
    assert vikunja.auth == {"Bearer key"}
    meta = frontmatter((tmp_path / "tickets" / "DIS-0031.md").read_text())[0]
    assert meta["thread"] == THREAD and meta["game"] == "se1" and meta["status"] == "open"
    assert meta["url"] == f"{vikunja.url}/tasks/3"
    assert (tmp_path / "tickets" / "completed" / "DIS-0030.md").exists()


def test_first_number_starts_an_empty_project(tracker, vikunja):
    tracker.config.first_number = 7
    assert tracker.create("ticket", "Crash", "Body", {}) == "DIS-0007"


def test_reports_go_to_open_tickets_only(tracker, vikunja):
    vikunja.add("DIS-0001: Open", header_html({"thread": THREAD}))
    vikunja.add("DIS-0002: Fixed", done=True)
    vikunja.add("DIS-0003: Dropped", done=True, labels=("cancelled",))
    tracker.add_report("DIS-0001", "Same here", {"thread": "https://discord.com/channels/1/2000"})
    [comment] = vikunja.comments[1]
    assert comment["comment"].startswith("<p><strong>Report</strong></p><ul>")
    assert "Same here" in comment["comment"] and "channels/1/2000" in comment["comment"]
    with pytest.raises(StoreError, match="DIS-0002 is completed. If the problem came back"):
        tracker.add_report("DIS-0002", "Again", {})
    with pytest.raises(StoreError, match="DIS-0003 is canceled"):
        tracker.add_report("DIS-0003", "Again", {})
    with pytest.raises(StoreError, match="no such ticket: DIS-0009"):
        tracker.add_report("DIS-0009", "Again", {})


def test_mirror_follows_the_tracker(tracker, vikunja, tmp_path):
    task = vikunja.add("DIS-0001: Crash", header_html({"thread": THREAD, "game": "se2"}) + "<p>Details</p>")
    tracker.refresh()
    text = (tmp_path / "tickets" / "DIS-0001.md").read_text()
    assert "\n# Description\n\nDetails\n" in text
    assert tracker.ticket_threads() == {"DIS-0001": THREAD}
    tracker.log_work("DIS-0001", "Fixed in Bugfixes 1.7.0 <b>now</b>")
    text = (tmp_path / "tickets" / "DIS-0001.md").read_text()
    assert "## bot-clangy at 2026-10-17T12:00:00Z\n\nWork log\n\nFixed in Bugfixes 1.7.0 <b>now</b>" in text
    task["done"] = True
    tracker.refresh()
    assert not (tmp_path / "tickets" / "DIS-0001.md").exists()
    assert (tmp_path / "tickets" / "completed" / "DIS-0001.md").exists()
    assert tracker.ticket_threads() == {"DIS-0001": THREAD}


def test_store_hands_tickets_to_the_tracker(tracker, vikunja, tmp_path):
    store = Store(tmp_path / "workspace", tmp_path / "records", tracker)
    assert store.create("ticket", "Crash", "Body", {"thread": THREAD}) == "DIS-0001"
    assert store.create("note", "A note", "Body", {}) == "N-0001"
    store.add_report("DIS-0001", "Again", {})
    assert store.tickets_dir == tmp_path / "tickets"
    assert not (tmp_path / "workspace" / "tickets").exists()
    with pytest.raises(StoreError, match="not a ticket id"):
        store.add_report("../x", "Again", {})


def test_html_in_reports_is_escaped(tracker, vikunja):
    tracker.create("ticket", "Crash", "Look: <script>alert(1)</script> and `a < b`", {})
    description = vikunja.tasks[1]["description"]
    assert "<script>" not in description and "&lt;script&gt;" in description and "<code>a &lt; b</code>" in description


def test_header_round_trip():
    meta = {"created": "2026-10-17T12:00:00Z", "thread": THREAD, "game": "se1", "ignored": "x"}
    assert parse_header(header_html(meta)) == {k: v for k, v in meta.items() if k != "ignored"}
    assert to_text(
        "<h1>Logs</h1>\n<p>a<br>b</p>\n<ul>\n<li>x</li>\n<li>y</li>\n</ul>\n<pre><code>1 &lt; 2</code></pre>"
    ) == ("# Logs\n\na\nb\n\n- x\n- y\n\n1 < 2")


def test_api_errors_become_store_errors(tracker, vikunja):
    tracker.config.project = 4
    with pytest.raises(StoreError, match="HTTP 403"):
        tracker.create("ticket", "Crash", "Body", {})


def test_notes_are_tasks_with_the_note_label_linked_to_the_tickets_they_name(tracker, vikunja, tmp_path):
    vikunja.add("DIS-0017: Aliases")
    vikunja.add("N-0011: Old note")
    note = tracker.create("note", "Finding", "Same as DIS-0017, not DIS-0099.", {"game": "se1"}, first_number=24)
    assert note == "N-0024"
    task = vikunja.tasks[3]
    assert task["title"] == "N-0024: Finding" and [l["title"] for l in task["labels"]] == ["se1", "note"]
    assert [x["id"] for x in task["related_tasks"]["related"]] == [1]
    assert vikunja.bucket_of == {3: 36}
    text = (tmp_path / "notes" / "N-0024.md").read_text()
    assert "related: DIS-0017\n" in text and "status:" not in text
    assert "related: N-0024\n" in (tmp_path / "tickets" / "DIS-0017.md").read_text()
    assert tracker.create("note", "Next", "Body", {}, first_number=1) == "N-0025"
    assert tracker.create("ticket", "Crash", "Body", {}) == "DIS-0018"


def test_large_text_is_gzipped_and_too_large_files_stop_the_write(tracker, vikunja, tmp_path):
    log, image = tmp_path / "info.log", tmp_path / "shot.png"
    log.write_text("2026-10-17 INFO all good\n" * 20000)
    image.write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + bytes(300 * 1024))  # Compresses well, but binary
    tracker.create("ticket", "Crash", "Body", {}, [(log, "Pulsar log"), (image, "Screenshot")])
    (_, log_name, packed), (_, image_name, raw) = vikunja.uploads
    assert log_name == "info.log.gz" and gzip.decompress(packed) == log.read_bytes()
    assert image_name == "shot.png" and raw == image.read_bytes()
    assert "<code>info.log.gz</code>: Pulsar log" in vikunja.tasks[1]["description"]
    tracker.config.max_file_bytes = 100 * 1024
    with pytest.raises(StoreError, match="shot.png is larger than"):
        tracker.create("ticket", "Crash", "Body", {}, [(image, "Screenshot")])
    assert len(vikunja.tasks) == 1


def test_reads_every_page_past_the_servers_cap(tracker, vikunja):
    for number in range(1, 121):
        vikunja.add(f"DIS-{number:04d}: Crash {number}")
    assert tracker.create("ticket", "Next", "Body", {}) == "DIS-0121"
    assert len(tracker.ticket_threads()) == 0 and tracker.refresh() == 121


def test_an_upload_vikunja_refuses_is_an_error(tracker, vikunja, tmp_path):
    vikunja.add("DIS-0001: Crash")
    rejected = tmp_path / "reject.log"
    rejected.write_text("x")
    with pytest.raises(StoreError, match="Vikunja didn't take reject.log"):
        tracker.add_report("DIS-0001", "Again", {}, [(rejected, "Log")])

import gzip
import runpy
import sys
from pathlib import Path

from clangy.config import TasksConfig
from clangy.store import Store
from clangy.tracker import Tracker
from tests.fakes import FakeVikunja

SCRIPT = Path(__file__).parent.parent / "scripts" / "migrate-tickets-to-vikunja.py"


def test_migration_copies_tickets_notes_reports_and_work_logs(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    store = Store(workspace, tmp_path / "records")
    log = tmp_path / "1015-info.log"
    log.write_text("2026-10-17 INFO all good\n" * 20000)
    meta = {"thread": "https://discord.com/channels/1/1015", "game": "se1"}
    store.create("ticket", "Crash", "## Summary\n\nIt crashes. See N-0002.", meta, [(log, "Pulsar log")])
    store.add_report("DIS-0001", "Same here", meta)
    store.log_work("DIS-0001", "2026-10-15: Fixed in Bugfixes 1.7.0", "Close", {})
    (workspace / "tickets" / "DIS-0001.md").rename(workspace / "tickets" / "completed" / "DIS-0001.md")
    store.create("ticket", "Dropped", "Body", {})
    (workspace / "tickets" / "DIS-0002.md").rename(workspace / "tickets" / "canceled" / "DIS-0002.md")
    store.create("note", "Finding", "Duplicates DIS-0002.", {"game": "se2"})
    config = tmp_path / "clangy.toml"
    vikunja = FakeVikunja()
    try:
        config.write_text(
            f'data_dir = "{tmp_path}"\nworkspace_dir = "{workspace}"\nbackends = []\n'
            f'[tasks]\nurl = "{vikunja.url}"\nproject = 8\napi_key = "key"\n'
        )
        for _ in range(2):  # The second run skips what is there
            monkeypatch.setattr(sys, "argv", ["migrate", "--config", str(config)])
            runpy.run_path(str(SCRIPT), run_name="__main__")
        titles = [t["title"] for t in vikunja.tasks.values()]
        assert titles == ["DIS-0001: Crash", "DIS-0002: Dropped", "N-0002: Finding"]
        first, second, note = vikunja.tasks[1], vikunja.tasks[2], vikunja.tasks[3]
        assert first["done"] and second["done"] and [l["title"] for l in second["labels"]] == ["cancelled"]
        assert "<li>notes: N-0001</li>" in first["description"] and "<h2>Summary</h2>" in first["description"]
        assert "tickets/completed/DIS-0001.md" in first["description"]
        [(task_id, name, content)] = vikunja.uploads
        assert (task_id, name) == (1, "1015-info.log.gz") and gzip.decompress(content) == log.read_bytes()
        files, report, work_log = [c["comment"] for c in vikunja.comments[1]]
        assert files.startswith("<p><strong>Note N-0001: Files of DIS-0001</strong></p>")
        assert report.startswith("<p><strong>Report</strong></p>") and "Same here" in report
        assert "Fixed in Bugfixes 1.7.0" in work_log
        assert not note["done"] and [l["title"] for l in note["labels"]] == ["se2", "note"]
        assert [x["id"] for x in note["related_tasks"]["related"]] == [2, 1]
        assert vikunja.bucket_of == {3: 36}
        tracker = Tracker(TasksConfig(url=vikunja.url, project=8, api_key="key"), tmp_path / "tickets", tmp_path / "n")
        assert tracker.ticket_threads() == {"DIS-0001": "https://discord.com/channels/1/1015"}
        assert (tmp_path / "tickets" / "canceled" / "DIS-0002.md").exists()
        assert (tmp_path / "notes" / "N-0002.md").exists()
    finally:
        vikunja.close()


def test_raw_attachments_are_swapped_for_gzipped_copies(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    store = Store(workspace, tmp_path / "records")
    log = tmp_path / "1015-info.log"
    log.write_text("2026-10-17 INFO all good\n" * 20000)
    store.create("ticket", "Crash", "Body", {}, [(log, "Pulsar log")])
    config = tmp_path / "clangy.toml"
    vikunja = FakeVikunja()
    try:
        config.write_text(
            f'data_dir = "{tmp_path}"\nworkspace_dir = "{workspace}"\nbackends = []\n'
            f'[tasks]\nurl = "{vikunja.url}"\nproject = 8\napi_key = "key"\n'
        )
        vikunja.add("DIS-0001: Crash")
        vikunja.handle("PUT", "/api/v1/tasks/1/attachments", _multipart("1015-info.log", log.read_bytes()), {})
        monkeypatch.setattr(sys, "argv", ["migrate", "--config", str(config)])
        runpy.run_path(str(SCRIPT), run_name="__main__")
        assert [a["file"]["name"] for a in vikunja.tasks[1]["attachments"]] == ["1015-info.log.gz"]
        assert gzip.decompress(vikunja.uploads[-1][2]) == log.read_bytes()
    finally:
        vikunja.close()


def _multipart(name: str, content: bytes) -> bytes:
    return (
        f'--b\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n\r\n'.encode()
        + content
        + b"\r\n--b--\r\n"
    )

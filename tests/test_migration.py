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
    log.write_bytes(b"log data")
    meta = {"thread": "https://discord.com/channels/1/1015", "game": "se1"}
    store.create("ticket", "Crash", "## Summary\n\nIt crashes.", meta, [(log, "Pulsar log")])
    store.add_report("DIS-0001", "Same here", meta)
    store.log_work("DIS-0001", "2026-10-15: Fixed in Bugfixes 1.7.0", "Close", {})
    (workspace / "tickets" / "DIS-0001.md").rename(workspace / "tickets" / "completed" / "DIS-0001.md")
    store.create("ticket", "Dropped", "Body", {})
    (workspace / "tickets" / "DIS-0002.md").rename(workspace / "tickets" / "canceled" / "DIS-0002.md")
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
        assert [t["title"] for t in vikunja.tasks.values()] == ["DIS-0001: Crash", "DIS-0002: Dropped"]
        first, second = vikunja.tasks[1], vikunja.tasks[2]
        assert first["done"] and second["done"] and [l["title"] for l in second["labels"]] == ["cancelled"]
        assert "<li>notes: N-0001</li>" in first["description"] and "<h2>Summary</h2>" in first["description"]
        assert "tickets/completed/DIS-0001.md" in first["description"]
        assert vikunja.uploads == [(1, "1015-info.log", b"log data")]
        note, report, work_log = [c["comment"] for c in vikunja.comments[1]]
        assert note.startswith("<p><strong>Note N-0001: Files of DIS-0001</strong></p>")
        assert report.startswith("<p><strong>Report</strong></p>") and "Same here" in report
        assert "Fixed in Bugfixes 1.7.0" in work_log
        mirror = Tracker(TasksConfig(url=vikunja.url, project=8, api_key="key"), tmp_path / "tickets")
        assert mirror.ticket_threads() == {"DIS-0001": "https://discord.com/channels/1/1015"}
        assert (tmp_path / "tickets" / "canceled" / "DIS-0002.md").exists()
    finally:
        vikunja.close()

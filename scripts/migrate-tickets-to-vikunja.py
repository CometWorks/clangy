"""Copies the workspace's tickets, with the notes that hold their files, into the Vikunja project of `[tasks]`.

    .venv/bin/python scripts/migrate-tickets-to-vikunja.py --config ~/clangy.toml [--dry-run]

Run it once, before Clangy starts with `[tasks]` configured. Tickets go in DIS order, so the task index matches the
number in an empty project. A ticket already in Vikunja (by its DIS id) is skipped, so a failed run can be repeated.
The workspace's files stay as they are.
"""

import argparse
import re
from pathlib import Path

from clangy.config import Config
from clangy.store import CLOSED, frontmatter
from clangy.tracker import PRIORITIES, TITLE_RE, Tracker, header_html, to_html

DATE = "2026-10-17"


def sections(body: str) -> tuple[str, list[str], str]:
    """A ticket body's description, its `## Report` sections and its work log."""
    body, _, work_log = body.partition("\n# Work log")
    description, *reports = re.split(r"(?m)^## Report\n", body)
    return description.strip().removeprefix("# Description").strip(), [r.strip() for r in reports], work_log.strip()


def ticket_notes(workspace: Path, doc_id: str, fields: dict[str, str]) -> list[Path]:
    """The ticket's notes: named in its `notes:`, or naming it in their `ticket:`."""
    ids = [n.strip() for n in fields.get("notes", "").split(",") if n.strip()]
    for main in [*workspace.glob("notes/N-*/NOTE.md"), *workspace.glob("notes/N-*.md")]:
        if frontmatter(main.read_text())[0].get("ticket") == doc_id:
            ids.append(main.parent.name if main.name == "NOTE.md" else main.stem)
    found = []
    for note_id in dict.fromkeys(ids):
        folder, file = workspace / "notes" / note_id, workspace / "notes" / f"{note_id}.md"
        found.append(folder / "NOTE.md" if folder.is_dir() else file)
    return found


def migrate(tracker: Tracker, workspace: Path, path: Path, dry_run: bool) -> None:
    fields, body = frontmatter(path.read_text())
    doc_id = fields["id"]
    status = path.parent.name if path.parent.name in CLOSED else "open"
    description, reports, work_log = sections(body)
    notes = ticket_notes(workspace, doc_id, fields)
    files = [f for n in notes if n.name == "NOTE.md" for f in sorted(n.parent.iterdir()) if f.name != "NOTE.md"]
    large = [f for f in files if f.stat().st_size > tracker.config.max_file_bytes]
    print(
        f"{doc_id} {status}: {len(reports)} reports, notes {[n.parent.name if n.name == 'NOTE.md' else n.stem for n in notes]}, {len(files)} files"
        + (f", too large: {[f.name for f in large]}" if large else ""),
        flush=True,
    )
    if dry_run:
        return

    origin = path.relative_to(workspace)
    markup = (
        header_html(fields)
        + f"<p><em>Migrated from Clangy's workspace ({origin}) on {DATE}.</em></p>"
        + to_html(description)
    )
    task = tracker.api(
        "PUT",
        f"/projects/{tracker.config.project}/tasks",
        {"title": f"{doc_id}: {fields['title']}", "description": markup},
    )
    task_id = task["id"]
    for label in [fields.get("game", "")] + ([tracker.config.canceled_label] if status == "canceled" else []):
        if label:
            tracker.label(task_id, label)
    for note in notes:
        note_fields, note_body = frontmatter(note.read_text())
        note_id = note_fields.get("id", note.stem)
        if note.name == "NOTE.md":
            for file in sorted(note.parent.iterdir()):
                if file.name != "NOTE.md" and file not in large:
                    tracker.upload(task_id, file, file.name)
        skipped = [f.name for f in large if f.parent == note.parent]
        extra = (
            f"<p>Not attached, over the size limit: {', '.join(skipped)}. They stay in notes/{note_id}/.</p>"
            if skipped
            else ""
        )
        tracker.comment(
            task_id,
            f"<p><strong>Note {note_id}: {note_fields.get('title', '')}</strong></p>" + to_html(note_body) + extra,
        )
    for report in reports:
        tracker.comment(task_id, "<p><strong>Report</strong></p>" + to_html(report))
    if work_log:
        tracker.comment(task_id, "<p><strong>Work log</strong></p>" + to_html(work_log))
    update = {}
    if fields.get("priority") in PRIORITIES:
        update["priority"] = PRIORITIES[fields["priority"]]
    if status != "open":
        update["done"] = True
    if update:
        tracker.update(task_id, **update)
    print(f"  -> task {task_id}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = Config.load(args.config)
    if config.tasks is None:
        raise SystemExit("the config has no [tasks]")
    workspace = config.workspace_dir or config.data_dir / "workspace"
    tracker = Tracker(config.tasks, config.data_dir / "tickets")
    present = {m[1] for t in tracker.tasks() if (m := TITLE_RE.match(t["title"]))}
    tickets = workspace / "tickets"
    paths = [p for d in (tickets, *(tickets / c for c in CLOSED)) for p in d.glob("DIS-[0-9][0-9][0-9][0-9].md")]
    for path in sorted(paths, key=lambda p: p.name):
        if path.stem in present:
            print(f"{path.stem} is in Vikunja already")
            continue
        migrate(tracker, workspace, path, args.dry_run)
    if not args.dry_run:
        tracker.refresh()


if __name__ == "__main__":
    main()

"""Copies the workspace's tickets and notes into the Vikunja project of `[tasks]`.

    .venv/bin/python scripts/migrate-tickets-to-vikunja.py --config ~/clangy.toml [--dry-run]

Run it before Clangy starts with `[tasks]` configured. Tickets go in DIS order, so the task index matches the number
in an empty project. A note that holds a ticket's files becomes a comment on the ticket, with the files attached. The
other notes become tasks of their own with the note label in the Notes bucket, linked to the tickets they name and the tickets naming them.
Large compressible files are attached gzipped; files attached raw by an earlier run are swapped for gzipped copies.
Tickets and notes already in Vikunja are skipped, so a failed run can be repeated. The workspace's files stay as they are.
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


def all_notes(workspace: Path) -> dict[str, Path]:
    """Note id to its main file."""
    mains = [
        *workspace.glob("notes/N-[0-9][0-9][0-9][0-9]/NOTE.md"),
        *workspace.glob("notes/N-[0-9][0-9][0-9][0-9].md"),
    ]
    return dict(sorted((m.parent.name if m.name == "NOTE.md" else m.stem, m) for m in mains))


def note_files(main: Path) -> list[Path]:
    return sorted(f for f in main.parent.iterdir() if f.name != "NOTE.md") if main.name == "NOTE.md" else []


def ticket_notes(notes: dict[str, Path], doc_id: str, fields: dict[str, str]) -> list[str]:
    """The ids of the notes that hold a ticket's files: named in its `notes:`, or naming it in their `ticket:`."""
    ids = [n.strip() for n in fields.get("notes", "").split(",") if n.strip()]
    ids += [note_id for note_id, main in notes.items() if frontmatter(main.read_text())[0].get("ticket") == doc_id]
    return [i for i in dict.fromkeys(ids) if i in notes]


def attach(tracker: Tracker, task_id: int, files: list[Path]) -> list[str]:
    """Attaches the files, gzipped where that helps; returns the ones too large even then."""
    skipped = []
    for file in files:
        try:
            data, name = tracker.packed(file.read_bytes(), file.name)
        except ValueError:
            skipped.append(file.name)
            continue
        tracker.upload(task_id, name, data)
    return skipped


def create(tracker: Tracker, title: str, markup: str) -> int:
    return tracker.api("PUT", f"/projects/{tracker.config.project}/tasks", {"title": title, "description": markup})[
        "id"
    ]


def migrate_ticket(tracker: Tracker, workspace: Path, notes: dict[str, Path], path: Path, dry_run: bool) -> None:
    fields, body = frontmatter(path.read_text())
    doc_id = fields["id"]
    status = path.parent.name if path.parent.name in CLOSED else "open"
    description, reports, work_log = sections(body)
    own = ticket_notes(notes, doc_id, fields)
    print(f"{doc_id} {status}: {len(reports)} reports, notes {own}", flush=True)
    if dry_run:
        return
    origin = path.relative_to(workspace)
    markup = header_html(fields) + f"<p><em>Migrated from Clangy's workspace ({origin}) on {DATE}.</em></p>"
    task_id = create(tracker, f"{doc_id}: {fields['title']}", markup + to_html(description))
    for label in [fields.get("game", "")] + ([tracker.config.canceled_label] if status == "canceled" else []):
        if label:
            tracker.label(task_id, label)
    for note_id in own:
        note_fields, note_body = frontmatter(notes[note_id].read_text())
        skipped = attach(tracker, task_id, note_files(notes[note_id]))
        extra = (
            f"<p>Not attached, too large: {', '.join(skipped)}. They stay in notes/{note_id}/.</p>" if skipped else ""
        )
        title = f"<p><strong>Note {note_id}: {note_fields.get('title', '')}</strong></p>"
        tracker.comment(task_id, title + to_html(note_body) + extra)
    for report in reports:
        tracker.comment(task_id, "<p><strong>Report</strong></p>" + to_html(report))
    if work_log:
        tracker.comment(task_id, "<p><strong>Work log</strong></p>" + to_html(work_log))
    update = {"priority": PRIORITIES[fields["priority"]]} if fields.get("priority") in PRIORITIES else {}
    if status != "open":
        update["done"] = True
    if update:
        tracker.update(task_id, **update)
    print(f"  -> task {task_id}", flush=True)


def migrate_note(tracker: Tracker, workspace: Path, note_id: str, main: Path, naming: list[str], dry_run: bool) -> None:
    fields, body = frontmatter(main.read_text())
    files = note_files(main)
    print(f"{note_id}: {len(files)} files, named by {naming}", flush=True)
    if dry_run:
        return
    origin = main.relative_to(workspace)
    markup = header_html(fields) + f"<p><em>Migrated from Clangy's workspace ({origin}) on {DATE}.</em></p>"
    task_id = create(tracker, f"{note_id}: {fields['title']}", markup + to_html(body))
    for label in (fields.get("game", ""), tracker.config.note_label):
        if label:
            tracker.label(task_id, label)
    if skipped := attach(tracker, task_id, files):
        tracker.comment(
            task_id, f"<p>Not attached, too large: {', '.join(skipped)}. They stay in {origin.parent}/.</p>"
        )
    tracker.move_to_bucket(task_id, tracker.config.notes_bucket)
    tracker.relate_mentions(task_id, " ".join([body, fields.get("ticket", ""), *naming]))
    print(f"  -> task {task_id}", flush=True)


def repack(tracker: Tracker, notes: dict[str, Path], dry_run: bool) -> None:
    """Swaps attachments that went up raw for gzipped copies, from the note files they came from."""
    local = {f.name: f for main in notes.values() for f in note_files(main)}
    for task in tracker.tasks():
        for attachment in task.get("attachments") or []:
            name, size = attachment["file"]["name"], attachment["file"]["size"]
            if name not in local or size < tracker.config.gzip_min_bytes or local[name].stat().st_size != size:
                continue
            data, packed_name = tracker.packed(local[name].read_bytes(), name)
            if packed_name == name:
                continue
            print(f"{task['title'][:9]} {name}: {size} -> {len(data)} bytes gzipped", flush=True)
            if not dry_run:
                tracker.upload(task["id"], packed_name, data)
                tracker.api("DELETE", f"/tasks/{task['id']}/attachments/{attachment['id']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = Config.load(args.config)
    if config.tasks is None:
        raise SystemExit("the config has no [tasks]")
    workspace = config.workspace_dir or config.data_dir / "workspace"
    tracker = Tracker(config.tasks, config.data_dir / "tickets", config.data_dir / "notes")
    present = {m[1] for t in tracker.tasks() if (m := TITLE_RE.match(t["title"]))}
    notes = all_notes(workspace)
    tickets = workspace / "tickets"
    paths = sorted(
        (p for d in (tickets, *(tickets / c for c in CLOSED)) for p in d.glob("DIS-[0-9][0-9][0-9][0-9].md")),
        key=lambda p: p.name,
    )
    texts = {p.stem: p.read_text() for p in paths}
    holding = {n for p in paths for n in ticket_notes(notes, p.stem, frontmatter(texts[p.stem])[0])}
    for path in paths:
        if path.stem not in present:
            migrate_ticket(tracker, workspace, notes, path, args.dry_run)
    tracker.refresh()  # The tickets' task ids, for the notes' links
    for note_id, main in notes.items():
        if note_id not in holding and note_id not in present:
            naming = [t for t, text in texts.items() if re.search(rf"\b{note_id}\b", text)]
            migrate_note(tracker, workspace, note_id, main, naming, args.dry_run)
    repack(tracker, notes, args.dry_run)
    if not args.dry_run:
        tracker.refresh()


if __name__ == "__main__":
    main()

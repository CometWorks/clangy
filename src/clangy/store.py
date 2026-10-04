"""Clangy's workspace of tickets, notes and plans, and the records of record-mode threads. Written only by the host."""

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

KINDS = {"ticket": ("tickets", "T", ""), "note": ("notes", "N", "NOTE.md"), "plan": ("plans", "P", "PLAN.md")}
"""Folder, id prefix, and the main file of a document that has its own folder for its files."""
CLOSED = ("completed", "canceled")
"""Subfolders of tickets that developers move finished tickets to."""
TEMPLATES = Path(__file__).parent / "templates"
TITLE_MAX = 120
BODY_MAX = 20000
ID_RE = re.compile(r"^[TNP]-\d{4}$")

KeptFile = tuple[Path, str]
"""A file to keep with a document, and its line in the list of files."""


class StoreError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _check_text(title: str, body: str) -> tuple[str, str]:
    title = _one_line(title)
    body = body.strip()
    if not title:
        raise StoreError("title is empty")
    if len(title) > TITLE_MAX:
        raise StoreError(f"title is longer than {TITLE_MAX} characters")
    if not body:
        raise StoreError("body is empty")
    if len(body) > BODY_MAX:
        raise StoreError(f"body is longer than {BODY_MAX} characters")
    return title, body


def _replace(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)


class Store:
    def __init__(self, root: Path, records: Path):
        self.root = root
        self.records = records
        for folder, _, _ in KINDS.values():
            (root / folder).mkdir(parents=True, exist_ok=True)
        for folder in CLOSED:
            (root / "tickets" / folder).mkdir(exist_ok=True)
        records.mkdir(parents=True, exist_ok=True)
        # Humans maintain these once they exist
        for source, target in (("agents.md", "AGENTS.md"), ("ticket.md", "tickets/Template.md")):
            if not (root / target).exists():
                shutil.copyfile(TEMPLATES / source, root / target)

    def create(self, kind: str, title: str, body: str, meta: dict[str, str], files: list[KeptFile] = ()) -> str:
        """Writes a new document with the next free number and returns its id.

        A note or plan with files gets its own folder for them. A ticket's files go into a new note it links to.
        """
        title, body = _check_text(title, body)
        if kind != "ticket":
            return self._write(kind, title, body, meta, files)
        ticket_id = self._write(kind, title, f"# Description\n\n{body}\n\n# Work log", meta)
        if files:
            self._files_note(ticket_id, meta, files)
        return ticket_id

    def add_report(self, ticket_id: str, body: str, meta: dict[str, str], files: list[KeptFile] = ()) -> None:
        """Adds a repeated report of the same problem to an open ticket, above the work log."""
        if not ID_RE.match(ticket_id) or not ticket_id.startswith("T-"):
            raise StoreError(f"not a ticket id: {ticket_id}")
        tickets = self.root / "tickets"
        path = tickets / f"{ticket_id}.md"
        if not path.exists():
            for folder in CLOSED:
                if (tickets / folder / f"{ticket_id}.md").exists():
                    raise StoreError(
                        f"{ticket_id} is {folder}. If the problem came back, create a new ticket and name {ticket_id} in it."
                    )
            raise StoreError(f"no such ticket: {ticket_id}")
        _, body = _check_text("report", body)
        meta = {"added": _now(), **meta}
        if files:
            meta["notes"] = self._files_note(ticket_id, meta, files)
        lines = "".join(f"- {k}: {_one_line(v)}\n" for k, v in meta.items())
        report = f"## Report\n\n{lines}\n{body}\n\n"
        text = path.read_text()
        at = text.find("\n# Work log")
        _replace(path, text.rstrip("\n") + "\n\n" + report if at < 0 else text[: at + 1] + report + text[at + 1 :])

    def append_record(self, thread_id: int, title: str, url: str, entry: str) -> None:
        """Records a message of a record-mode forum thread for later recall."""
        path = self.records / f"{thread_id}.md"
        if not path.exists():
            path.write_text(f"---\ntitle: {_one_line(title)}\nthread: {url}\n---\n\n")
        with open(path, "a") as f:
            f.write(entry.rstrip() + "\n\n")

    def _files_note(self, ticket_id: str, meta: dict[str, str], files: list[KeptFile]) -> str:
        """Keeps a ticket's files in a new note, since the conversation's own copies expire, and links it."""
        note_meta = {k: v for k, v in meta.items() if k in ("thread", "game")}
        note_id = self._write(
            "note", f"Files of {ticket_id}", f"Files of {ticket_id}.", {**note_meta, "ticket": ticket_id}, files
        )
        path = self.root / "tickets" / f"{ticket_id}.md"
        head, sep, rest = path.read_text().partition("\n---\n")
        lines = head.split("\n")
        notes = next((i for i, line in enumerate(lines) if line.startswith("notes: ")), None)
        if notes is None:
            lines.append(f"notes: {note_id}")
        else:
            lines[notes] += f", {note_id}"
        _replace(path, "\n".join(lines) + sep + rest)
        return note_id

    def _write(self, kind: str, title: str, body: str, meta: dict[str, str], files: list[KeptFile] = ()) -> str:
        folder, prefix, main = KINDS[kind]
        directory = self.root / folder
        searched = [directory, *(directory / c for c in CLOSED if kind == "ticket")]
        taken = (p.name[2:6] for d in searched for p in d.glob(f"{prefix}-[0-9][0-9][0-9][0-9]*"))
        number = 1 + max((int(n) for n in taken), default=0)
        if files:
            body += "\n\n# Files\n\n" + "".join(f"- `{target}`: {line}\n" for target, line in _targets(files))
        while True:
            doc_id = f"{prefix}-{number:04d}"
            number += 1
            header = {"id": doc_id, "title": title, "created": _now(), **meta}
            if kind == "ticket":
                header.setdefault("status", "new")
            text = "---\n" + "".join(f"{k}: {_one_line(v)}\n" for k, v in header.items()) + f"---\n\n{body}\n"
            # mkdir and O_EXCL keep numbers unique even if two writers race
            if files:
                if (directory / f"{doc_id}.md").exists():
                    continue
                try:
                    (directory / doc_id).mkdir()
                except FileExistsError:
                    continue
                for (source, _), (target, _) in zip(files, _targets(files)):
                    shutil.copyfile(source, directory / doc_id / target)
                (directory / doc_id / main).write_text(text)
                return doc_id
            if (directory / doc_id).exists():
                continue
            try:
                fd = os.open(directory / f"{doc_id}.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                continue
            with os.fdopen(fd, "w") as f:
                f.write(text)
            return doc_id


def _targets(files: list[KeptFile]) -> list[tuple[str, str]]:
    """File names in the document's folder, unique and never its main file, with their lines."""
    names: list[tuple[str, str]] = []
    for source, line in files:
        name, number = source.name, 1
        while name in ("NOTE.md", "PLAN.md") or any(name == taken for taken, _ in names):
            number += 1
            name = f"{source.stem}-{number}{source.suffix}"
        names.append((name, line))
    return names

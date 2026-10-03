"""Clangy's own store of tickets, notes, plans and records, written only by the host."""

import os
import re
from datetime import datetime, timezone
from pathlib import Path

KINDS = {"ticket": ("tickets", "T"), "note": ("notes", "N"), "plan": ("plans", "P")}
TITLE_MAX = 120
BODY_MAX = 20000
ID_RE = re.compile(r"^[TNP]-\d{4}$")


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


class Store:
    def __init__(self, root: Path):
        self.root = root
        for folder, _ in KINDS.values():
            (root / folder).mkdir(parents=True, exist_ok=True)
        (root / "records").mkdir(parents=True, exist_ok=True)

    def create(self, kind: str, title: str, body: str, meta: dict[str, str]) -> str:
        """Writes a new document with the next free number and returns its id."""
        folder, prefix = KINDS[kind]
        title, body = _check_text(title, body)
        directory = self.root / folder
        number = 1 + max((int(p.stem[2:]) for p in directory.glob(f"{prefix}-[0-9][0-9][0-9][0-9].md")), default=0)
        while True:
            doc_id = f"{prefix}-{number:04d}"
            header = {"id": doc_id, "title": title, "created": _now(), **meta}
            if kind == "ticket":
                header.setdefault("status", "new")
            text = "---\n" + "".join(f"{k}: {_one_line(v)}\n" for k, v in header.items()) + f"---\n\n{body}\n"
            try:
                # O_EXCL keeps numbers unique even if two writers race
                fd = os.open(directory / f"{doc_id}.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                number += 1
                continue
            with os.fdopen(fd, "w") as f:
                f.write(text)
            return doc_id

    def add_report(self, ticket_id: str, body: str, meta: dict[str, str]) -> None:
        """Appends a repeated report of the same problem to an existing ticket."""
        if not ID_RE.match(ticket_id) or not ticket_id.startswith("T-"):
            raise StoreError(f"not a ticket id: {ticket_id}")
        path = self.root / "tickets" / f"{ticket_id}.md"
        if not path.exists():
            raise StoreError(f"no such ticket: {ticket_id}")
        _, body = _check_text("report", body)
        lines = "".join(f"- {k}: {_one_line(v)}\n" for k, v in {"added": _now(), **meta}.items())
        with open(path, "a") as f:
            f.write(f"\n## Report\n\n{lines}\n{body}\n")

    def append_record(self, thread_id: int, title: str, url: str, entry: str) -> None:
        """Records a message of a record-mode forum thread for later recall."""
        path = self.root / "records" / f"{thread_id}.md"
        if not path.exists():
            path.write_text(f"---\ntitle: {_one_line(title)}\nthread: {url}\n---\n\n")
        with open(path, "a") as f:
            f.write(entry.rstrip() + "\n\n")

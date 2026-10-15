"""Clangy's workspace of tickets, notes and plans, and the records of record-mode threads. Written only by the host."""

import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

KINDS = {"ticket": ("tickets", "DIS", ""), "note": ("notes", "N", "NOTE.md"), "plan": ("plans", "P", "PLAN.md")}
"""Folder, id prefix, and the main file of a document that has its own folder for its files."""
CLOSED = ("completed", "canceled")
"""Subfolders of tickets that developers move finished tickets to."""
TEMPLATES = Path(__file__).parent / "templates"
TITLE_MAX = 120
BODY_MAX = 20000
ID_RE = re.compile(r"^(DIS|N|P)-\d{4}$")
HISTORY = ".history.git"
"""The workspace's git repository, in its root, which the sandbox can't see. Only the host commits."""
TRACKED = "/*\n!/AGENTS.md\n!/tickets/\n!/notes/\n!/plans/\n*.tmp\n"
"""Ignore rules of the history: the workspace root may be a home folder."""

log = logging.getLogger(__name__)

KeptFile = tuple[Path, str]
"""A file to keep with a document, and its line in the list of files."""


class StoreError(ValueError):
    pass


def utc_now() -> str:
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


def frontmatter(text: str) -> tuple[dict[str, str], str]:
    """The `key: value` lines between the `---` lines at the start of a document, and the text after them."""
    if not text.startswith("---\n"):
        return {}, text.strip()
    head, _, body = text[4:].partition("\n---\n")
    return dict(line.split(": ", 1) for line in head.splitlines() if ": " in line), body.strip()


def document(meta: dict[str, str], body: str) -> str:
    return "---\n" + "".join(f"{k}: {_one_line(v)}\n" for k, v in meta.items()) + f"---\n\n{body}\n"


def write_file(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)


class History:
    """Git history of the workspace, so a lost or overwritten document can be brought back.

    The repository has no .git in the work tree and lives where the sandbox can't read or write it. After every
    commit it is pushed to its `origin` remote, if one is set up.
    """

    def __init__(self, work_tree: Path):
        self.work_tree = work_tree
        self.git_dir = work_tree / HISTORY
        self._lock = threading.Lock()
        self._push_lock = threading.Lock()
        """Pushes have their own lock: a slow one must not hold up commits, which run on the event loop."""

    def commit(self, message: str) -> None:
        """Commits every change to the workspace; a failure is logged, the document is written anyway."""
        try:
            with self._lock:
                if not (self.git_dir / "HEAD").exists():
                    self._git("init", "--quiet", "--bare", "--initial-branch=main", str(self.git_dir), init=True)
                (self.git_dir / "info").mkdir(exist_ok=True)
                (self.git_dir / "info" / "exclude").write_text(TRACKED)
                self._git("add", "--all")
                if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
                    return
                self._git("commit", "--quiet", "--message", message)
                pushed = bool(self._git("remote").stdout.strip())
        except (OSError, subprocess.SubprocessError) as e:
            log.error("History commit failed: %s %s", e, getattr(e, "stderr", "") or "")
            return
        if pushed:
            threading.Thread(target=self._push, daemon=True).start()

    def _push(self) -> None:
        try:
            with self._push_lock:
                self._git("push", "--quiet", "origin", "main", timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            log.error("History push failed: %s %s", e, getattr(e, "stderr", "") or "")

    def _git(
        self, *args: str, check: bool = True, init: bool = False, timeout: float = 60
    ) -> subprocess.CompletedProcess:
        where = [] if init else [f"--git-dir={self.git_dir}", f"--work-tree={self.work_tree}"]
        settings = [
            "user.name=Clangy",
            "user.email=clangy@localhost",
            "commit.gpgsign=false",
            "core.hooksPath=/dev/null",
        ]
        command = ["git", *where, *(a for s in settings for a in ("-c", s)), *args]
        return subprocess.run(command, cwd=self.work_tree, check=check, capture_output=True, text=True, timeout=timeout)


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
        self.history = History(root)
        self.history.commit("Changes made outside Clangy")

    def create(
        self, kind: str, title: str, body: str, meta: dict[str, str], files: list[KeptFile] = (), by: str = ""
    ) -> str:
        """Writes a new document with the next free number and returns its id.

        A note or plan with files gets its own folder for them. A ticket's files go into a new note it links to.
        `by` is the user who asked for it, for the history.
        """
        title, body = _check_text(title, body)
        self.history.commit("Changes made outside Clangy")
        if kind != "ticket":
            doc_id = self._write(kind, title, body, meta, files)
        else:
            doc_id = self._write(kind, title, f"# Description\n\n{body}\n\n# Work log", meta)
            if files:
                self._files_note(doc_id, meta, files)
        self.history.commit(_message(f"Create {doc_id}: {title}", by, meta))
        return doc_id

    def add_report(
        self, ticket_id: str, body: str, meta: dict[str, str], files: list[KeptFile] = (), by: str = ""
    ) -> None:
        """Adds a repeated report of the same problem to an open ticket, above the work log."""
        if not ID_RE.match(ticket_id) or not ticket_id.startswith("DIS-"):
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
        self.history.commit("Changes made outside Clangy")
        message = _message(f"Add a report to {ticket_id}", by, meta)
        meta = {"added": utc_now(), **meta}
        if files:
            meta["notes"] = self._files_note(ticket_id, meta, files)
        lines = "".join(f"- {k}: {_one_line(v)}\n" for k, v in meta.items())
        report = f"## Report\n\n{lines}\n{body}\n\n"
        text = path.read_text()
        at = text.find("\n# Work log")
        write_file(path, text.rstrip("\n") + "\n\n" + report if at < 0 else text[: at + 1] + report + text[at + 1 :])
        self.history.commit(message)

    def ticket_threads(self) -> dict[str, str]:
        """The thread link of every ticket, open or closed."""
        return {p.stem: meta["thread"] for p in self._tickets() if "thread" in (meta := frontmatter(p.read_text())[0])}

    def log_work(self, ticket_id: str, line: str, subject: str, meta: dict[str, str]) -> None:
        """Adds a line to the end of a ticket's work log, open or closed, with `subject` for the history."""
        path = next((p for p in self._tickets() if p.stem == ticket_id), None)
        if path is None:
            raise StoreError(f"no such ticket: {ticket_id}")
        self.history.commit("Changes made outside Clangy")
        write_file(path, path.read_text().rstrip("\n") + f"\n- {_one_line(line)}\n")
        self.history.commit(_message(subject, "", meta))

    def _tickets(self) -> list[Path]:
        tickets = self.root / "tickets"
        return [p for d in (tickets, *(tickets / c for c in CLOSED)) for p in d.glob("DIS-[0-9][0-9][0-9][0-9].md")]

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
        write_file(path, "\n".join(lines) + sep + rest)
        return note_id

    def _write(self, kind: str, title: str, body: str, meta: dict[str, str], files: list[KeptFile] = ()) -> str:
        folder, prefix, main = KINDS[kind]
        directory = self.root / folder
        searched = [directory, *(directory / c for c in CLOSED if kind == "ticket")]
        start = len(prefix) + 1
        taken = (p.name[start : start + 4] for d in searched for p in d.glob(f"{prefix}-[0-9][0-9][0-9][0-9]*"))
        number = 1 + max((int(n) for n in taken), default=0)
        if files:
            body += "\n\n# Files\n\n" + "".join(f"- `{target}`: {line}\n" for target, line in _targets(files))
        while True:
            doc_id = f"{prefix}-{number:04d}"
            number += 1
            header = {"id": doc_id, "title": title, "created": utc_now(), **meta}
            if kind == "ticket":
                header.setdefault("status", "new")
            text = document(header, body)
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


def _message(subject: str, by: str, meta: dict[str, str]) -> str:
    details = [f"Discord user: {by}"] if by else []
    details += [f"Thread: {meta['thread']}"] if "thread" in meta else []
    return "\n\n".join([subject, "\n".join(details)]) if details else subject


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

"""Tickets in a Vikunja project, and their read-only Markdown mirror for the sandbox.

Adapted from the Ticket Manager's `vklib.py`. Calls block, so the host runs them in a thread.
"""

import gzip
import html
import json
import logging
import os
import re
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import markdown

from clangy.config import TasksConfig
from clangy.store import (
    CLOSED,
    KeptFile,
    StoreError,
    _check_text,
    _one_line,
    _targets,
    document,
    frontmatter,
    utc_now,
    write_file,
)

log = logging.getLogger(__name__)

TITLE_RE = re.compile(r"^((DIS|N)-\d{4}): (.*)$", re.S)
"""A ticket or note: its id, the id's prefix and the title."""
TICKET_REF_RE = re.compile(r"\bDIS-\d{4}\b")
HEADER_RE = re.compile(r"<li>(\w+): (.*?)</li>")
LINK_RE = re.compile(r'<a href="([^"]*)"[^>]*>.*?</a>', re.S)
HEADER_KEYS = ("created", "thread", "game", "notes", "ticket", "renamed_from", "severity")
"""Frontmatter fields kept in the list at the top of a task's description."""
PRIORITIES = {"low": 1, "medium": 2, "high": 3}


class TrackerError(StoreError):
    pass


def to_html(text: str) -> str:
    """Markdown as HTML. HTML in the text is escaped: it comes from Discord users through the model."""
    md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists"])
    md.preprocessors.deregister("html_block")
    md.inlinePatterns.deregister("html")
    return md.convert(text)


def to_text(markup: str | None) -> str:
    """Vikunja's HTML as plain Markdown-ish text, for grep."""
    if not markup:
        return ""
    s = LINK_RE.sub(r"\1", markup)
    s = re.sub(r"(?i)<h([1-6])[^>]*>", lambda m: "#" * int(m[1]) + " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(li|tr)>\s*", "\n", s)
    s = re.sub(r"(?i)</(p|div|h[1-6]|pre|blockquote|ul|ol|table)>\s*", "\n\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "- ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def header_html(meta: dict[str, str]) -> str:
    """The frontmatter fields as the list a task's description starts with; links stay clickable."""
    items = []
    for key in HEADER_KEYS:
        if value := _one_line(meta.get(key, "")):
            value = html.escape(value)
            if value.startswith("https://"):
                value = f'<a href="{value}">{value}</a>'
            items.append(f"<li>{key}: {value}</li>")
    return f"<ul>{''.join(items)}</ul>"


def parse_header(description: str) -> dict[str, str]:
    head = description.split("</ul>", 1)[0] if description.startswith("<ul>") else ""
    return {k: html.unescape(LINK_RE.sub(r"\1", v)) for k, v in HEADER_RE.findall(head)}


Upload = tuple[str, str, bytes]
"""A file as attached: its name, its line in the list of files, and its content."""


def files_html(uploads: list[Upload]) -> str:
    rows = "".join(f"<li><code>{html.escape(name)}</code>: {html.escape(line)}</li>" for name, line, _ in uploads)
    return f"<p>Attached files:</p><ul>{rows}</ul>"


def bash_variable(name: str) -> str:
    """A variable only an interactive bash has: the nas' ~/.bashrc exports the key there."""
    r = subprocess.run(
        ["bash", "-ic", f'printf "\\n%s" "${name}"'], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    return r.stdout.rsplit("\n", 1)[-1].strip()


class Tracker:
    """The tickets and notes of one Vikunja project. Titles start with the DIS or N id, which Clangy assigns.

    Notes carry the note label, which the project's Kanban view filters out.
    """

    def __init__(self, config: TasksConfig, mirror: Path, notes_mirror: Path):
        self.config = config
        self.url = config.url.rstrip("/")
        self.mirror = mirror
        self.notes_mirror = notes_mirror
        self._key: str | None = config.api_key
        self._lock = threading.RLock()
        self._ids: dict[str, int] = {}
        """DIS and N ids to Vikunja task ids, from the last refresh."""
        self._labels: dict[str, int] | None = None

    # HTTP

    def _token(self) -> str:
        if not self._key:
            self._key = os.environ.get(self.config.api_key_env, "").strip() or bash_variable(self.config.api_key_env)
        if not self._key:
            raise TrackerError(f"no Vikunja API key: set tasks.api_key or {self.config.api_key_env}")
        return self._key

    def api(self, method: str, path: str, body=None, *, data: bytes | None = None, content_type: str = ""):
        if body is not None:
            data, content_type = json.dumps(body).encode(), "application/json"
        request = urllib.request.Request(f"{self.url}/api/v1{path}", data=data, method=method)
        request.add_header("Authorization", "Bearer " + self._token())
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")[:300]
            raise TrackerError(f"Vikunja {method} {path}: HTTP {e.code} {text}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TrackerError(f"Vikunja {method} {path}: {e}") from None
        return json.loads(raw) if raw else None

    def packed(self, data: bytes, name: str) -> tuple[bytes, str]:
        """The file as it is attached: gzipped if it's large text that compresses well, like a log."""
        if len(data) >= self.config.gzip_min_bytes and b"\0" not in data[:8192]:  # Images keep their preview
            packed = gzip.compress(data, mtime=0)
            if len(packed) <= len(data) * 0.7:
                data, name = packed, name + ".gz"
        if len(data) > self.config.max_file_bytes:
            raise StoreError(f"{name} is larger than the {self.config.max_file_bytes // 1000**2} MB the tracker takes")
        return data, name

    def uploads(self, files: list[KeptFile]) -> list[Upload]:
        """The files packed for attaching, before anything is written, so a file too large stops the write."""
        out = []
        for (source, _), (target, line) in zip(files, _targets(files)):
            data, name = self.packed(source.read_bytes(), target)
            out.append((name, line, data))
        return out

    def upload(self, task_id: int, name: str, content: bytes) -> None:
        boundary = uuid.uuid4().hex
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        )
        data = head.encode() + content + f"\r\n--{boundary}--\r\n".encode()
        self.api(
            "PUT", f"/tasks/{task_id}/attachments", data=data, content_type=f"multipart/form-data; boundary={boundary}"
        )

    # Vikunja objects

    def tasks(self) -> list[dict]:
        """Every task of the project, open and done."""
        out, page = [], 1
        while True:
            batch = self.api("GET", f"/tasks?per_page=250&page={page}") or []
            out.extend(batch)
            if len(batch) < 250:
                return [t for t in out if t["project_id"] == self.config.project]
            page += 1

    def comments(self, task_id: int) -> list[dict]:
        return self.api("GET", f"/tasks/{task_id}/comments?per_page=250") or []

    def comment(self, task_id: int, markup: str) -> None:
        self.api("PUT", f"/tasks/{task_id}/comments", {"comment": markup})

    def label(self, task_id: int, title: str) -> None:
        if self._labels is None:
            self._labels = {l["title"]: l["id"] for l in self.api("GET", "/labels?per_page=250") or []}
        if title in self._labels:
            try:
                self.api("PUT", f"/tasks/{task_id}/labels", {"label_id": self._labels[title]})
            except TrackerError as e:
                if "HTTP 409" not in str(e):  # Already has it
                    raise
        else:
            log.warning("Vikunja has no label %r", title)

    def update(self, task_id: int, **fields) -> dict:
        # An update replaces the whole task, so it goes on top of the current one
        task = self.api("GET", f"/tasks/{task_id}")
        task.update(fields)
        return self.api("POST", f"/tasks/{task_id}", task)

    def attach(self, task_id: int, uploads: list[Upload]) -> None:
        for name, _, content in uploads:
            self.upload(task_id, name, content)

    # Tickets

    def create(
        self, kind: str, title: str, body: str, meta: dict[str, str], files: list[KeptFile] = (), first_number: int = 1
    ) -> str:
        """Files a new ticket or note with the next free number and returns the id. A note is linked to the tickets
        it names. `first_number` is the lowest number a note may get, after the workspace's own."""
        title, body = _check_text(title, body)
        uploads = self.uploads(files)
        prefix = "DIS" if kind == "ticket" else "N"
        first = self.config.first_number if kind == "ticket" else first_number
        with self._lock:
            taken = [
                int(m[1].split("-")[1]) for t in self.tasks() if (m := TITLE_RE.match(t["title"])) and m[2] == prefix
            ]
            doc_id = f"{prefix}-{max(first, 1 + max(taken, default=0)):04d}"
            description = header_html({"created": utc_now(), **meta}) + to_html(body)
            if uploads:
                description += files_html(uploads)
            task = self.api(
                "PUT",
                f"/projects/{self.config.project}/tasks",
                {"title": f"{doc_id}: {title}", "description": description},
            )
            self._ids[doc_id] = task["id"]
            # The document exists now; a failure would only make the model create it again
            try:
                labels = [meta.get("game", "")] + ([self.config.note_label] if kind == "note" else [])
                for label in filter(None, labels):
                    self.label(task["id"], label)
                self.attach(task["id"], uploads)
                if kind == "note":
                    self.relate_mentions(task["id"], body)
            except TrackerError as e:
                log.error("%s is created without its labels, files or links: %s", doc_id, e)
            self._refresh_after_write()
        return doc_id

    def relate_mentions(self, task_id: int, text: str) -> None:
        """Links the task to every ticket the text names."""
        named = list(dict.fromkeys(TICKET_REF_RE.findall(text)))
        if any(t not in self._ids for t in named):
            self._refresh()
        for ticket_id in named:
            if (other := self._ids.get(ticket_id)) and other != task_id:
                try:
                    self.api("PUT", f"/tasks/{task_id}/relations", {"other_task_id": other, "relation_kind": "related"})
                except TrackerError as e:
                    if "HTTP 409" not in str(e):  # Linked already
                        raise

    def add_report(self, ticket_id: str, body: str, meta: dict[str, str], files: list[KeptFile] = ()) -> None:
        """Adds a repeated report of the same problem to an open ticket, as a comment."""
        _, body = _check_text("report", body)
        uploads = self.uploads(files)
        with self._lock:
            task = self._task(ticket_id)
            if task["done"]:
                state = "canceled" if self._canceled(task) else "completed"
                raise StoreError(
                    f"{ticket_id} is {state}. If the problem came back, create a new ticket and name {ticket_id} in it."
                )
            markup = "<p><strong>Report</strong></p>" + header_html({"created": utc_now(), **meta}) + to_html(body)
            if uploads:
                markup += files_html(uploads)
            self.attach(task["id"], uploads)
            self.comment(task["id"], markup)
            self._refresh_after_write()

    def log_work(self, ticket_id: str, line: str) -> None:
        with self._lock:
            self.comment(self._task(ticket_id)["id"], f"<p><strong>Work log</strong></p><p>{html.escape(line)}</p>")
            self._refresh_after_write()

    def ticket_threads(self) -> dict[str, str]:
        """The thread link of every ticket in the mirror, open or closed."""
        return {p.stem: meta["thread"] for p in self._mirrored() if "thread" in (meta := frontmatter(p.read_text())[0])}

    def _task(self, ticket_id: str) -> dict:
        if ticket_id not in self._ids:
            self._refresh()
        if ticket_id not in self._ids:
            raise StoreError(f"no such ticket: {ticket_id}")
        return self.api("GET", f"/tasks/{self._ids[ticket_id]}")

    def _canceled(self, task: dict) -> bool:
        return any(l["title"] == self.config.canceled_label for l in task.get("labels") or [])

    # The mirror

    def refresh(self) -> int:
        """Rewrites the mirrors from Vikunja and returns the number of tickets and notes. Keeps the old one if Vikunja can't be
        read."""
        with self._lock:
            self._refresh()
            return len(self._ids)

    def _refresh_after_write(self) -> None:
        try:
            self._refresh()
        except TrackerError as e:
            log.warning("Tickets not refreshed: %s", e)

    def _refresh(self) -> None:
        # Files are replaced one by one: a running sandbox has the folders themselves mounted
        tasks = [(m[1], m[3], t) for t in self.tasks() if (m := TITLE_RE.match(t["title"]))]
        for folder in (self.mirror, *(self.mirror / c for c in CLOSED), self.notes_mirror):
            folder.mkdir(parents=True, exist_ok=True)
        written = set()
        for doc_id, title, task in tasks:
            if doc_id.startswith("N-"):
                path, status = self.notes_mirror / f"{doc_id}.md", None
            else:
                status = "open" if not task["done"] else "canceled" if self._canceled(task) else "completed"
                path = self.mirror / ("" if status == "open" else status) / f"{doc_id}.md"
            write_file(path, self._document(doc_id, title, status, task))
            written.add(path)
        for path in [*self._mirrored(), *self.notes_mirror.glob("N-[0-9][0-9][0-9][0-9].md")]:
            if path not in written:
                path.unlink()
        self._ids = {doc_id: task["id"] for doc_id, _, task in tasks}

    def _document(self, doc_id: str, title: str, status: str | None, task: dict) -> str:
        description = task.get("description") or ""
        meta = {"id": doc_id, "title": title, **parse_header(description), **({"status": status} if status else {})}
        meta["url"] = f"{self.url}/tasks/{task['id']}"
        related = sorted(x["title"].split(":", 1)[0] for x in (task.get("related_tasks") or {}).get("related") or [])
        if related:
            meta["related"] = ", ".join(related)
        body = to_text(description.split("</ul>", 1)[1] if description.startswith("<ul>") else description)
        if task.get("attachments"):
            body += "\n\n# Attachments\n\n" + "".join(f"- `{a['file']['name']}`\n" for a in task["attachments"])
        comments = [
            f"## {c['author']['username']} at {c['created']}\n\n{to_text(c['comment'])}"
            for c in self.comments(task["id"])
        ]
        return document(meta, f"# Description\n\n{body.strip()}\n\n# Comments\n\n" + "\n\n".join(comments))

    def _mirrored(self) -> list[Path]:
        return [
            p for d in (self.mirror, *(self.mirror / c for c in CLOSED)) for p in d.glob("DIS-[0-9][0-9][0-9][0-9].md")
        ]

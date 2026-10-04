"""Files the agent sends back: validation, sending each file once, zipping groups of related files."""

import os
import re
import shutil
import zipfile
from pathlib import Path

ZIP_ABOVE = 3
"""Groups of related files larger than this are sent as one ZIP archive."""
MAX_FILES = 10
"""Discord's attachment limit per message."""
MAX_BYTES = 5 * 1024**2
"""Largest file the agent may send, also stated in the attach_files tool description."""


class FileError(ValueError):
    pass


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".")[:100] or "file"


class Outbox:
    """Files staged for the answer of one run. They reach Discord only if the run succeeds."""

    def __init__(self, work: Path, staging: Path, sent: set[str], total_bytes: int, markers: list[str]):
        self.work = work.resolve()
        self.staging = staging
        self.sent = sent
        self.total_bytes = total_bytes
        self.markers = [m.lower().encode() for m in markers]
        self.files: list[Path] = []
        self.sources: list[str] = []
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)

    def add(self, paths: list[str], archive: str | None = None) -> str:
        """Stages one group of related files and returns a note for the agent."""
        if not paths:
            raise FileError("no files given")
        sources = [self._source(p) for p in paths]
        names = [s.relative_to(self.work).as_posix() for s in sources]
        for name in names:
            if name in self.sent or name in self.sources:
                raise FileError(f"{name} was already sent, write a changed version under a new name")
        if len(set(names)) < len(names):
            raise FileError("a file is listed twice")
        if len(sources) > ZIP_ABOVE:
            staged = [self._zip(sources, archive or "files.zip")]
        else:
            staged = [self._copy(s) for s in sources]
        self._check_limits(staged)
        self.files += staged
        self.sources += names
        if len(sources) > ZIP_ABOVE:
            return f"{staged[0].name} ({len(sources)} files) will be attached to your answer."
        return f"{', '.join(names)} will be attached to your answer."

    def _source(self, path: str) -> Path:
        source = (self.work / path).resolve()
        if not source.is_relative_to(self.work):
            raise FileError(f"{path} is outside your work folder")
        relative = source.relative_to(self.work)
        if any(part.startswith(".") for part in relative.parts):
            raise FileError(f"{path} is hidden and can't be sent")
        if not source.is_file():
            raise FileError(f"{path} is not a file")
        if source.stat().st_size > MAX_BYTES:
            raise FileError(f"{path} is larger than {MAX_BYTES // 1024**2} MB")
        content = source.read_bytes().lower()
        if any(marker in content for marker in self.markers):
            raise FileError(f"{path} can't be shared")
        return source

    def _unique(self, name: str) -> Path:
        target = self.staging / safe_filename(name)
        stem, suffix = target.stem, target.suffix
        number = 1
        while target.exists():
            number += 1
            target = self.staging / f"{stem}-{number}{suffix}"
        return target

    def _copy(self, source: Path) -> Path:
        target = self._unique(source.name)
        shutil.copyfile(source, target)
        return target

    def _zip(self, sources: list[Path], name: str) -> Path:
        target = self._unique(name if name.lower().endswith(".zip") else f"{name}.zip")
        root = Path(os.path.commonpath([s.parent for s in sources]))
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for source in sources:
                archive.write(source, source.relative_to(root).as_posix())
        return target

    def _check_limits(self, staged: list[Path]) -> None:
        problem = None
        if len(self.files) + len(staged) > MAX_FILES:
            problem = f"at most {MAX_FILES} attachments fit in an answer"
        elif any(f.stat().st_size > MAX_BYTES for f in staged):
            problem = f"the archive is larger than {MAX_BYTES // 1024**2} MB"
        elif sum(f.stat().st_size for f in self.files + staged) > self.total_bytes:
            problem = f"the attachments of an answer must stay under {self.total_bytes // 1024**2} MB in total"
        if problem is not None:
            for file in staged:
                file.unlink()
            raise FileError(problem)

    def commit(self, sent_dir: Path) -> list[Path]:
        """Moves the staged files to the retained sent files and returns their new paths."""
        sent_dir.mkdir(parents=True, exist_ok=True)
        moved = []
        for file in self.files:
            target = sent_dir / file.name
            file.replace(target)
            moved.append(target)
        self.sent.update(self.sources)
        return moved

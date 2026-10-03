"""Daily knowledge sync: mirrors the skill and book repositories and swaps in a fresh tree atomically."""

import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from clangy.config import Config

log = logging.getLogger(__name__)

KEEP_RELEASES = 2
"""The current tree and the previous one, which running sandboxes may still read."""


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _mirror(url: str, mirrors: Path) -> tuple[Path, str]:
    """Clones or fetches a mirror of the repository and returns it with its HEAD commit."""
    path = mirrors / re.sub(r"[^A-Za-z0-9._-]", "_", url)
    if path.exists():
        _git("-C", str(path), "remote", "update", "--prune")
    else:
        _git("clone", "--quiet", "--mirror", url, str(path))
    return path, _git("-C", str(path), "rev-parse", "HEAD")


def _export(mirror: Path, target: Path) -> None:
    target.mkdir(parents=True)
    archive = subprocess.Popen(["git", "-C", str(mirror), "archive", "--format=tar", "HEAD"], stdout=subprocess.PIPE)
    assert archive.stdout is not None
    with tarfile.open(fileobj=archive.stdout, mode="r|") as tar:
        tar.extractall(target, filter="data")
    if archive.wait() != 0:
        raise subprocess.CalledProcessError(archive.returncode, "git archive")


def sync(config: Config) -> bool:
    """Updates the knowledge tree if any repository changed. Returns whether it swapped in a new one."""
    base = config.data_dir / "knowledge"
    mirrors = base / "mirrors"
    mirrors.mkdir(parents=True, exist_ok=True)
    local: dict[str, Path] = {}
    heads: dict[str, str] = {}
    for repos in config.knowledge.values():
        for repo in repos:
            local[repo.url], heads[repo.url] = _mirror(repo.url, mirrors)

    current = base / "current"
    state_file = current / "state.json"
    if state_file.exists() and json.loads(state_file.read_text()) == heads:
        log.info("Knowledge is up to date")
        return False

    releases = base / "releases"
    release = releases / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    for game, repos in config.knowledge.items():
        for repo in repos:
            _export(local[repo.url], release / game / repo.mount)
    (release / "state.json").write_text(json.dumps(heads, indent=2))

    link = base / "current.tmp"
    link.unlink(missing_ok=True)
    link.symlink_to(release.relative_to(base), target_is_directory=True)
    os.replace(link, current)
    log.info("Knowledge updated to %s", release.name)

    for old in sorted(releases.iterdir())[:-KEEP_RELEASES]:
        shutil.rmtree(old)
    return True

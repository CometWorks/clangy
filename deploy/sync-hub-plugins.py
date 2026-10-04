#!/usr/bin/env python3
"""Clones every plugin registered in the plugin hubs at its registered commit, for the plugin skills.

Each hub is a git clone in a plugin skill's data folder. Every GitHub plugin it lists is cloned into
~/repos/<folder>/<owner>/<repo> and checked out at the hub's <Commit>. The skill's Data/Sources/<repo>
becomes a link to that clone, so the skill lists the plugin as downloaded and never fetches it itself.
A skill that keeps a record of its downloads gets the clones recorded there too. Sources the hubs don't
list are left alone. The skill's code index is rebuilt when anything changed.

~/repos/clone-or-pull runs it daily. Exits non-zero if any hub or plugin failed.
"""

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HOME = Path.home()
SKILLS = [
    {
        "skill": HOME / "repos/CometWorks/skills/skills/se-dev-plugin",
        "index": ["uv", "run", "--quiet", "index_plugins.py"],
        "hubs": [("PluginHub", "pulsar-se1-plugins"), ("MagnetarHub", "magnetar-plugins")],
    },
    {
        "skill": HOME / "repos/CometWorks/skills2/skills/se2-dev-plugin",
        "index": ["uv", "run", "--quiet", "index_plugin_code.py"],
        "hubs": [("PluginHub-SE2", "pulsar-se2-plugins")],
        # This skill keeps a record of its downloads and offers to download whatever isn't in it
        "record": True,
    },
]

RECORD = """
import json, sys
from plugin_registry import update_plugin
for plugin in json.load(sys.stdin):
    update_plugin(**plugin)
"""

failed: list[str] = []


def git(*args: str, cwd: Path | None = None) -> bool:
    # cat-file only probes whether a commit is there yet, its complaint isn't an error
    quiet = subprocess.DEVNULL if args[0] == "cat-file" else None
    return subprocess.run(["git", *args], cwd=cwd, stdout=subprocess.DEVNULL, stderr=quiet).returncode == 0


def head(clone: Path) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True)
    return result.stdout.strip()


def registered(hub: Path) -> list[tuple[str, str, ET.Element]]:
    """Owner/Repo, commit and XML of every GitHub plugin in the hub. MagnetarHub style has the repo in <RepoId>."""
    plugins = []
    for xml in sorted((hub / "Plugins").glob("*.xml")):
        root = ET.parse(xml).getroot()
        ref = (root.findtext("RepoId") or root.findtext("Id") or "").strip()
        commit = (root.findtext("Commit") or "").strip()
        if ref.count("/") == 1 and commit:
            plugins.append((ref, commit, root))
    return plugins


def checkout(ref: str, commit: str, clone: Path) -> bool:
    """Brings the clone to the commit. Returns whether it changed."""
    if not clone.exists() and not git("clone", "--quiet", f"https://github.com/{ref}.git", str(clone)):
        raise RuntimeError("clone failed")
    if head(clone) == commit:
        return False
    if not git("cat-file", "-e", f"{commit}^{{commit}}", cwd=clone):
        # A commit the hub pins may be on any branch, GitHub serves it by hash
        git("fetch", "--quiet", "--tags", "origin", cwd=clone)
        if not git("cat-file", "-e", f"{commit}^{{commit}}", cwd=clone) and not git(
            "fetch", "--quiet", "origin", commit, cwd=clone
        ):
            raise RuntimeError(f"commit {commit} not found")
    if not git("checkout", "--quiet", "--detach", commit, cwd=clone):
        raise RuntimeError(f"checkout of {commit} failed")
    return True


def link(sources: Path, repo: str, clone: Path) -> bool:
    """Points Sources/<repo> at the clone. Returns whether it changed."""
    path = sources / repo
    if path.is_symlink():
        if path.readlink() == clone:
            return False
        path.unlink()
    elif path.exists():
        # A copy the skill downloaded itself, superseded by the shared clone
        subprocess.run(["rm", "-rf", str(path)], check=True)
    path.symlink_to(clone, target_is_directory=True)
    return True


for entry in SKILLS:
    data = entry["skill"] / "Data"
    sources = data / "Sources"
    sources.mkdir(exist_ok=True)
    changed = False
    linked: dict[str, str] = {}
    records = []
    done: set[Path] = set()
    for hub_name, folder in entry["hubs"]:
        hub = data / hub_name
        print(f"Pulling {hub_name}")
        if not git("pull", "--quiet", "--ff-only", cwd=hub):
            failed.append(hub_name)
        for ref, commit, xml in registered(hub):
            owner, repo = ref.split("/")
            clone = HOME / "repos" / folder / owner / repo
            if clone in done:
                # Some hubs list one repo several times, e.g. per Pulsar flavor: the first entry's commit wins
                continue
            done.add(clone)
            clone.parent.mkdir(parents=True, exist_ok=True)
            try:
                changed |= checkout(ref, commit, clone)
            except RuntimeError as e:
                print(f"  {ref}: {e}", file=sys.stderr)
                failed.append(ref)
                continue
            if repo in linked:
                # In two hubs, or two owners' repos with one name: the first hub's clone keeps the link
                if linked[repo] != ref:
                    print(f"  {ref}: Sources/{repo} already links {linked[repo]}", file=sys.stderr)
                continue
            linked[repo] = ref
            changed |= link(sources, repo, clone)
            records.append(
                {
                    "key": repo,
                    "id": (xml.findtext("Id") or "").strip(),
                    "repo_id": ref,
                    "name": (xml.findtext("FriendlyName") or repo).strip(),
                    "registered_commit": commit,
                    "downloaded_commit": commit,
                    "method": "git",
                    "local_path": str(sources / repo),
                }
            )
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    if entry.get("record"):
        command = ["uv", "run", "--quiet", "python", "-c", RECORD]
        if subprocess.run(command, cwd=entry["skill"], env=env, input=json.dumps(records), text=True).returncode != 0:
            failed.append(f"download records of {entry['skill'].name}")
    if changed:
        print(f"Indexing {entry['skill'].name}")
        if subprocess.run(entry["index"], cwd=entry["skill"], env=env, stdout=subprocess.DEVNULL).returncode != 0:
            failed.append(f"index of {entry['skill'].name}")

if failed:
    print(f"Failed: {', '.join(failed)}", file=sys.stderr)
    sys.exit(1)

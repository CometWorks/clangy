import subprocess
from pathlib import Path

from clangy.config import BackendConfig, Config, KnowledgeRepo
from clangy.knowledge import sync


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_repo(path: Path, filename: str) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    commit(path, filename, "v1")
    return path


def commit(repo: Path, filename: str, content: str) -> None:
    (repo / filename).write_text(content)
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", content)


def test_sync_swaps_in_new_tree_only_on_change(tmp_path):
    skills = make_repo(tmp_path / "skills", "SKILL.md")
    books = make_repo(tmp_path / "books", "BOOK.md")
    config = Config(
        data_dir=tmp_path / "data",
        backends=[BackendConfig(name="b", url="http://x/v1", model="m")],
        knowledge={
            "se1": [KnowledgeRepo(url=str(skills), mount="skills"), KnowledgeRepo(url=str(books), mount="books")]
        },
    )
    current = config.data_dir / "knowledge" / "current"
    assert sync(config)
    first = current.resolve()
    assert (current / "se1" / "skills" / "SKILL.md").read_text() == "v1"
    assert (current / "se1" / "books" / "BOOK.md").read_text() == "v1"
    assert not (current / "se1" / "skills" / ".git").exists()

    assert not sync(config)
    assert current.resolve() == first

    commit(books, "BOOK.md", "v2")
    assert sync(config)
    assert (current / "se1" / "books" / "BOOK.md").read_text() == "v2"
    assert (first / "se1" / "books" / "BOOK.md").read_text() == "v1", "the previous tree stays for running sandboxes"

    commit(books, "BOOK.md", "v3")
    assert sync(config)
    assert not first.exists()
    assert len(list((config.data_dir / "knowledge" / "releases").iterdir())) == 2

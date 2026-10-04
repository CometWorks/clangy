import io
import zipfile

import pytest

from clangy.files import MAX_BYTES, FileError, Outbox


@pytest.fixture
def work(tmp_path):
    work = tmp_path / "work"
    (work / "out").mkdir(parents=True)
    return work


def outbox(work, tmp_path, sent=(), total=10 * 1024**2) -> Outbox:
    return Outbox(work, tmp_path / "staging", set(sent), total, ["knowledge/books"])


def write(work, name: str, content: bytes = b"data") -> str:
    path = work / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return name


def test_small_groups_are_sent_as_is(work, tmp_path):
    box = outbox(work, tmp_path)
    names = [write(work, f"out/{n}.txt") for n in "abc"]
    assert box.add(names) == "out/a.txt, out/b.txt, out/c.txt will be attached to your answer."
    assert [f.name for f in box.files] == ["a.txt", "b.txt", "c.txt"]


def test_groups_of_more_than_three_are_zipped(work, tmp_path):
    box = outbox(work, tmp_path)
    names = [write(work, f"out/logs/{n}.log", n.encode()) for n in "abcd"]
    assert box.add(names, "logs") == "logs.zip (4 files) will be attached to your answer."
    [archive] = box.files
    with zipfile.ZipFile(io.BytesIO(archive.read_bytes())) as z:
        assert sorted(z.namelist()) == ["a.log", "b.log", "c.log", "d.log"]
        assert z.read("c.log") == b"c"
    # A separate group of unrelated files stays separate
    box.add([write(work, "out/report.md")])
    assert [f.name for f in box.files] == ["logs.zip", "report.md"]


def test_each_file_is_sent_once(work, tmp_path):
    write(work, "out/a.txt")
    box = outbox(work, tmp_path, sent=["out/old.txt"])
    box.add(["out/a.txt"])
    with pytest.raises(FileError, match="already sent"):
        box.add(["out/a.txt"])
    write(work, "out/old.txt")
    with pytest.raises(FileError, match="already sent"):
        box.add(["out/old.txt"])
    sent_dir = tmp_path / "sent" / "0001"
    [moved] = box.commit(sent_dir)
    assert moved == sent_dir / "a.txt" and moved.read_bytes() == b"data"
    assert box.sent == {"out/a.txt", "out/old.txt"}


@pytest.mark.parametrize(
    ("path", "error"),
    [
        ("../escape.txt", "outside your work folder"),
        (".pi/system.md", "hidden"),
        ("out/missing.txt", "not a file"),
        ("out/big.bin", "larger than 5 MB"),
        ("out/secret.md", "can't be shared"),
        ("link.txt", "outside your work folder"),
    ],
)
def test_rejected_files(work, tmp_path, path, error):
    write(work, ".pi/system.md")
    write(work, "out/big.bin", b"x" * (MAX_BYTES + 1))
    write(work, "out/secret.md", b"see Knowledge/Books/x")
    (tmp_path / "escape.txt").write_text("x")
    (work / "link.txt").symlink_to(tmp_path / "escape.txt")
    with pytest.raises(FileError, match=error):
        outbox(work, tmp_path).add([path])


def test_answer_limits(work, tmp_path):
    box = outbox(work, tmp_path, total=10)
    box.add([write(work, "out/a.txt", b"12345678")])
    with pytest.raises(FileError, match="in total"):
        box.add([write(work, "out/b.txt", b"123")])
    assert [f.name for f in box.files] == ["a.txt"]
    assert sorted(p.name for p in (tmp_path / "staging").iterdir()) == ["a.txt"]

    box = outbox(work, tmp_path)
    for i in range(10):
        box.add([write(work, f"out/{i}.txt")])
    with pytest.raises(FileError, match="at most 10"):
        box.add([write(work, "out/eleven.txt")])

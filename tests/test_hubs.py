import pytest

import xml.etree.ElementTree as ET

from clangy.hubs import HubSupport, classify, parse_support


@pytest.mark.parametrize(
    "url, kind",
    [
        ("https://discord.com/users/123456789", "user"),
        ("https://discordapp.com/users/123456789/", "user"),
        ("https://discord.com/channels/1/2", "channel"),
        ("https://discord.gg/abcDEF", "invite"),
        ("https://discord.com/invite/abcDEF", "invite"),
        ("https://github.com/owner/repo/issues", "issues"),
        ("https://github.com/owner/repo/issues/new/choose", "issues"),
        ("https://github.com/owner/repo", "link"),
        ("https://discord.com/users/notanumber", "link"),
        ("http://example.com/help", "link"),
    ],
)
def test_support_link_kinds(url, kind):
    assert classify(url).kind == kind


@pytest.mark.parametrize("url", ["", "   ", "mailto:a@b.c", "discord://users/1", "javascript:alert(1)", "https://a b"])
def test_other_links_are_ignored(url):
    assert classify(url) is None


def test_user_link_carries_the_id():
    assert classify(" https://discord.com/users/42 ").user_id == 42
    assert classify("https://discord.gg/x").user_id is None


def write(hub, name, body):
    (hub / "Plugins").mkdir(exist_ok=True)
    (hub / "Plugins" / name).write_text(body)


def test_hub_support_reads_both_hub_styles_and_reloads_on_change(tmp_path):
    hub = tmp_path / "hub"
    hub.mkdir()
    write(hub, "a.xml", "<P><Id>Owner/Repo</Id><Support>https://discord.gg/x</Support></P>")
    write(
        hub, "b.xml", "<P><Id>guid</Id><RepoId>Other/Thing</RepoId><Support>https://discord.com/users/5</Support></P>"
    )
    write(hub, "c.xml", "<P><RepoId>No/Support</RepoId></P>")
    write(hub, "d.xml", "<broken")
    support = HubSupport([hub, tmp_path / "missing"])
    assert support.lookup("owner/repo").contact("bug").kind == "invite"
    assert support.lookup("OTHER/thing").contact("urgent").user_id == 5
    assert support.lookup("no/support") is None
    write(hub, "c.xml", "<P><RepoId>No/Support</RepoId><Support>https://discord.com/users/9</Support></P>")
    assert support.lookup("no/support").default.user_id == 9


def parse(xml):
    return parse_support(ET.fromstring(xml))


def test_simple_form_is_the_default_and_a_user_pings():
    entry = parse("<Support> https://discord.com/users/5 </Support>")
    assert entry.contact("question") is entry.default and entry.default.ping and entry.note == ""


def test_structured_form_matches_severity_then_default():
    entry = parse(
        "<Support>"
        "<Contact>https://example.com/any</Contact>"
        '<Contact severity="Bug">https://github.com/o/p/issues</Contact>'
        '<Contact severity="bogus">https://example.com/bogus</Contact>'
        '<Contact severity="urgent">mailto:a@b.c</Contact>'
        f"<Note>{'x' * 400}</Note>"
        "</Support>"
    )
    assert entry.contact("bug").kind == "issues"
    assert entry.contact("question").url == "https://example.com/any"
    assert entry.contact("urgent").url == "https://example.com/any"
    assert len(entry.note) == 300


def test_structured_form_without_a_default_has_no_contact_for_other_severities():
    entry = parse('<Support><Contact severity="bug">https://example.com/b</Contact></Support>')
    assert entry.contact("question") is None and entry.contact("bug").url == "https://example.com/b"
    assert parse("<Support><Note>only a note</Note></Support>") is None
    assert parse_support(None) is None


def test_user_pings_only_with_the_opt_in_in_the_structured_form():
    entry = parse(
        "<Support>"
        '<Contact severity="bug">https://discord.com/users/1</Contact>'
        '<Contact severity="urgent" ping="TRUE">https://discord.com/users/2</Contact>'
        "</Support>"
    )
    assert not entry.contact("bug").ping and entry.contact("urgent").ping

"""How to reach a plugin's author, from the <Support> element of its PluginHub entry."""

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

SupportKind = Literal["user", "channel", "invite", "issues", "link"]

SEVERITIES = ("question", "bug", "urgent")
"""From low to high: a question the docs and source don't answer, a reproducible problem for this player, a plugin
that is broken for everyone."""

NOTE_MAX = 300

KIND_NAMES: dict[SupportKind, str] = {
    "user": "Discord user",
    "channel": "Discord channel",
    "invite": "Discord server invite",
    "issues": "GitHub issue tracker",
    "link": "web page",
}

_DISCORD = r"(?:www\.)?(?:discord|discordapp)\.com"
_USER = re.compile(rf"https?://{_DISCORD}/users/(\d+)/?", re.IGNORECASE)
_CHANNEL = re.compile(rf"https?://{_DISCORD}/channels/(?:\d+|@me)/\d+(?:/\d+)?/?", re.IGNORECASE)
_INVITE = re.compile(rf"https?://(?:discord\.gg/|{_DISCORD}/invite/)[\w-]+/?", re.IGNORECASE)
_ISSUES = re.compile(r"https?://(?:www\.)?github\.com/[\w.-]+/[\w.-]+/issues(?:[/?#].*)?", re.IGNORECASE)


@dataclass(frozen=True)
class Support:
    kind: SupportKind
    url: str
    user_id: int | None = None
    """The Discord user, for kind `user` only."""
    ping: bool = False
    """Whether the author asked to be mentioned. Only a `user` link can be."""


@dataclass
class PluginSupport:
    """What a plugin's entry says about reaching its author."""

    default: Support | None = None
    """For every severity that has no contact of its own."""
    by_severity: dict[str, Support] = field(default_factory=dict)
    note: str = ""
    """The author's own words on how they like to be reached."""

    def contact(self, severity: str) -> Support | None:
        return self.by_severity.get(severity) or self.default


def classify(url: str, ping: bool = True) -> Support | None:
    """What a <Support> link is. Anything that isn't an http(s) link is ignored. `ping` is whether a Discord user link
    may be mentioned."""
    url = url.strip()
    if m := _USER.fullmatch(url):
        return Support("user", url, int(m.group(1)), ping)
    for kind, pattern in (("channel", _CHANNEL), ("invite", _INVITE), ("issues", _ISSUES)):
        if pattern.fullmatch(url):
            return Support(kind, url)
    if re.match(r"https?://[^\s/]+", url, re.IGNORECASE) and not re.search(r"\s", url):
        return Support("link", url)
    return None


def parse_support(element: ET.Element | None) -> PluginSupport | None:
    """The simple form `<Support>URL</Support>` is the contact for every severity, and a Discord user in it is
    mentioned. The structured form has <Contact severity="..." ping="true"> children and an optional <Note>; a
    Contact without a known severity is the default, and a user is mentioned only with ping="true"."""
    if element is None:
        return None
    contacts = element.findall("Contact")
    if not contacts:
        link = classify(element.text or "")
        return PluginSupport(default=link) if link else None
    result = PluginSupport(note=" ".join((element.findtext("Note") or "").split())[:NOTE_MAX])
    for contact in contacts:
        link = classify(contact.text or "", (contact.get("ping") or "").strip().lower() == "true")
        severity = (contact.get("severity") or "").strip().lower()
        if link is None or severity and severity not in SEVERITIES:
            continue
        if severity:
            result.by_severity.setdefault(severity, link)
        elif result.default is None:
            result.default = link
    if not (result.default or result.by_severity):
        return None
    return result


class HubSupport:
    """Maps a plugin's lowercase `owner/repo` to its <Support> in the hub clones' Plugins/*.xml.

    The files are read again when any of them changes, so the daily hub pull is picked up without a restart. A hub
    that can't be read has no contacts."""

    def __init__(self, hubs: list[Path]):
        self.hubs = hubs
        self._stamp: tuple | None = None
        self._support: dict[str, PluginSupport] = {}

    def lookup(self, repo: str) -> PluginSupport | None:
        files = [f for hub in self.hubs for f in sorted((hub / "Plugins").glob("*.xml"))]
        stamp = tuple((f, f.stat().st_mtime_ns) for f in files if f.exists())
        if stamp != self._stamp:
            self._support = self._read(files)
            self._stamp = stamp
        return self._support.get(repo.strip().lower())

    def _read(self, files: list[Path]) -> dict[str, PluginSupport]:
        for hub in self.hubs:
            if not (hub / "Plugins").is_dir():
                log.warning("Plugin hub %s has no Plugins folder, so its plugins have no support contact", hub)
        support: dict[str, PluginSupport] = {}
        for file in files:
            try:
                root = ET.parse(file).getroot()
            except (ET.ParseError, OSError) as e:
                log.warning("Can't read %s: %s", file, e)
                continue
            ref = (root.findtext("RepoId") or root.findtext("Id") or "").strip().lower()
            link = parse_support(root.find("Support"))
            if ref.count("/") == 1 and link:
                # Some hubs list one repo several times, e.g. per Pulsar flavor: the first entry wins
                support.setdefault(ref, link)
        return support

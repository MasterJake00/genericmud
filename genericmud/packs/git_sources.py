"""Soundpacks fetched straight from their own git repo (github/gitlab/gitea).

Some vault packs ship only a Windows installer whose real payload is a public git repo the
installer clones (Erion: ``gitlab.com/erion1/soundpack``). genericMud already *follows* the
installer to that repo -- but only after downloading and scanning the ~50 MB installer, and it
lands with the temp-dir pack id ``source``. This hand-curated registry names the repo directly,
so setup can skip the installer, pull the pack from its source in one step, and record a real
pack id + origin.

Deliberately tiny, like :mod:`genericmud.packs.manifest_sources`: one entry per MUD whose vault
download is only an installer wrapping a git repo. An unknown pack just falls back to the
installer-follow path in the vault browser.
"""

from __future__ import annotations

from dataclasses import dataclass

from genericmud.config.worlds import World


@dataclass(frozen=True)
class GitSource:
    """A soundpack whose canonical home is a public git repo."""

    id: str  # pack id and packs_dir/<id> subdir; must be a slug (see PackManifest.validate)
    name: str  # human label shown in the vault browser
    mud: str  # the MUD this pack is for (matched against a vault pack's MUD/name column)
    repo_url: str  # the pack's git repo (github/gitlab/gitea); a clone/web URL, ``.git`` optional
    entry: str  # load script relative to the repo root (the archive's wrapper dir is stripped)
    world: World  # curated connection (the installer bundles more than one world)
    dialect: str = "mushclient"  # one of manifest.KNOWN_DIALECTS
    archive_urls: tuple[str, ...] = ()  # nonstandard/self-hosted forge archive endpoints


# Erion's installer clones gitlab.com/erion1/soundpack (~1 GB: a portable MUSHclient install).
# The world file is MUSHclient/worlds/Erion MUD.mcl; connection is Erion MUD's published telnet
# endpoint. Fetching the archive directly skips the installer .exe entirely.
_SOURCES: dict[str, GitSource] = {
    # The vault's preserved `chatpack.zip` currently contains the project web page, not an
    # archive. Chatpack publishes the actual tree on its own GitLab instance.
    "chatpack": GitSource(
        id="chatpack",
        name="Chatpack",
        mud="ChatMud",
        repo_url="https://git.chatmud.com/athlon/chatpack",
        entry="chatpack/worlds/ChatMud.MCL",
        world=World(name="ChatMud", host="chatmud.com", port=7777),
        archive_urls=(
            "https://git.chatmud.com/athlon/chatpack/-/archive/master/chatpack-master.zip",
            "https://git.chatmud.com/athlon/chatpack/-/archive/main/chatpack-main.zip",
        ),
    ),
    "erion": GitSource(
        id="erion",
        name="Erion Mud Soundpack",
        mud="Erion MUD",
        repo_url="https://gitlab.com/erion1/soundpack",
        entry="MUSHclient/worlds/Erion MUD.mcl",
        world=World(name="Erion", host="erionmud.com", port=1234),
    ),
}


def _norm(text: str) -> str:
    """Lowercase, alphanumerics only: 'Erion MUD' == 'erion-mud' == 'ErionMUD'."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def all_sources() -> list[GitSource]:
    return list(_SOURCES.values())


def by_id(source_id: str) -> GitSource | None:
    return _SOURCES.get(source_id)


def for_labels(*labels: str) -> GitSource | None:
    """The git source matching any of ``labels`` (a vault pack's MUD or name), or None.

    Matched case- and punctuation-insensitively against each source's ``mud`` and ``name``, so a
    vault entry ("Erion Mud Soundpack" / MUD "Erion MUD") resolves to its repo.
    """
    targets = {_norm(label) for label in labels if label}
    for source in _SOURCES.values():
        if _norm(source.mud) in targets or _norm(source.name) in targets or source.id in targets:
            return source
    return None


@dataclass(frozen=True)
class UpdateSource:
    """Where an installed pack's files come from, for updating it file by file."""

    id: str  # the installed pack's id
    name: str  # human label for messages
    repo_url: str  # the pack's git repository (https)
    entry: str  # the pack's load script, relative to the repository's root


# Packs installed from their vault zips whose upstream is a public git repository. Their
# installs stay as they are (the vault browser still fetches the zip); updates read the
# repository. The entry tells the updater where the repository's root sits in the install:
# Toastush's zip kept GitHub's "PsudoDeSudo-Toastush-e8cdb7d/" folder around everything.
_UPDATE_ONLY: dict[str, UpdateSource] = {
    "miriani-next": UpdateSource(
        id="miriani-next",
        name="Miriani-Next",
        repo_url="https://github.com/distantorigin/miriani-next.git",
        entry="worlds/Miriani.MCL",
    ),
    "toastush": UpdateSource(
        id="toastush",
        name="Toastush",
        repo_url="https://github.com/PsudoDeSudo/Toastush.git",
        entry="worlds/Miriani.MCL",
    ),
    "atlan-s-mystavaria-pack": UpdateSource(
        id="atlan-s-mystavaria-pack",
        name="Atlan's Mystavaria Pack",
        repo_url="https://github.com/derbruedi/mystavaria-soundpack.git",
        entry="mystavaria/worlds/plugins/mystavaria.xml",
    ),
}


def update_source(pack_id: str) -> UpdateSource | None:
    """Where to update an installed pack from, file by file, or None when it has no known
    git repository."""
    source = _SOURCES.get(pack_id)
    if source is not None:
        return UpdateSource(source.id, source.name, _git_url(source.repo_url), source.entry)
    return _UPDATE_ONLY.get(pack_id)


def _git_url(repo_url: str) -> str:
    """A repository's clone address: its web address with .git on the end."""
    trimmed = repo_url.rstrip("/")
    return trimmed if trimmed.endswith(".git") else trimmed + ".git"

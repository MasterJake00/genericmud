#!/usr/bin/env python3
"""Extract a version's release notes from CHANGELOG.md.

The release workflow runs this to fill the GitHub release *body* at tag time. That body
is not decoration: ``self_update.check_for_update`` returns it as ``notes``, and that is
the text the in-app update dialog reads out to the user. Before this, every
release carried the same hardcoded "unzip the folder and run genericMud.exe" boilerplate,
so someone being offered an update was told how to install the client they already had
and nothing about what had changed in it.

A release's notes are its own section followed by the earlier versions' under their
headings, newest first. The update window of genericMud 0.20.3 and older shows only the
newest release's notes, so someone a few versions behind heard about the newest change
alone; newer ones show just the versions the player doesn't have yet
(``self_update.notes_since``).

Usage:
    extract_changelog.py <changelog_path> <version> [output_path]

``version`` accepts the release-tag form (``v0.7.1``) or the bare number (``0.7.1``).
Writes the notes to ``output_path`` as UTF-8 when given, else prints them to stdout.
Exits 1 when no section matches, so tagging without writing the notes fails loudly
instead of shipping a release that says nothing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Headings are ``## X.Y.Z — <date>``. Anchor on the H2 and a 3-part version; the
# lookahead stops ``0.7.1`` matching ``0.7.10`` or any longer prefix.
_VERSION_HEADING = re.compile(r"^##\s+(\d+\.\d+\.\d+)(?=\s|$)")

_USAGE = "usage: extract_changelog.py <changelog_path> <version> [output_path]"
# How much of the earlier versions' notes a release carries, in characters: whole sections, newest
# first, while they fit. GitHub takes up to 125,000.
_NOTES_LIMIT = 60_000
OLDER_NOTES = "Notes for older versions are on the releases page."


def extract_section(changelog: str, version: str) -> str | None:
    """Return the notes under the heading for ``version``, or None if there is no section.

    The heading line itself is dropped (the release page already shows the tag) and
    surrounding blank lines are trimmed. The section runs to the next version heading, or
    to end of file for the newest entry.
    """
    version = version.lstrip("v")
    lines = changelog.splitlines()
    start = None
    for i, line in enumerate(lines):
        heading = _VERSION_HEADING.match(line)
        if heading and heading.group(1) == version:
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if _VERSION_HEADING.match(lines[j]):
            end = j
            break
    return "\n".join(lines[start:end]).strip() or None


def _sections(changelog: str) -> list[tuple[str, str, str]]:
    """Every version's (version, heading line, notes), in the file's order: newest first."""
    lines = changelog.splitlines()
    starts = [
        (i, heading.group(1)) for i, line in enumerate(lines)
        if (heading := _VERSION_HEADING.match(line))
    ]
    sections = []
    for number, (start, version) in enumerate(starts):
        end = starts[number + 1][0] if number + 1 < len(starts) else len(lines)
        body = "\n".join(lines[start + 1:end]).strip()
        sections.append((version, lines[start].strip(), body))
    return sections


def extract_notes(changelog: str, version: str, limit: int = _NOTES_LIMIT) -> str | None:
    """The release notes for ``version``: its own section without its heading, then each
    earlier version's under its heading, while whole sections fit in ``limit`` characters;
    past that, a line saying where the rest is. None when ``version`` has no section."""
    own = extract_section(changelog, version)
    if own is None:
        return None
    version = version.lstrip("v")
    sections = _sections(changelog)
    position = next(index for index, (number, _h, _b) in enumerate(sections) if number == version)
    parts = [own]
    size = len(own)
    for _number, heading, body in sections[position + 1:]:
        block = f"{heading}\n\n{body}" if body else heading
        if size + len(block) + 2 > limit:
            parts.append(OLDER_NOTES)
            break
        parts.append(block)
        size += len(block) + 2
    return "\n\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not 2 <= len(argv) <= 3:
        print(_USAGE, file=sys.stderr)
        return 2
    changelog_path, version = argv[0], argv[1]
    output_path = argv[2] if len(argv) == 3 else None

    section = extract_notes(Path(changelog_path).read_text(encoding="utf-8"), version)
    if not section:
        print(f"No CHANGELOG section found for version {version!r}", file=sys.stderr)
        return 1

    if output_path:
        Path(output_path).write_text(section + "\n", encoding="utf-8")
    else:
        sys.stdout.write(section + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Text a trigger's pattern can't match without: a quick look before running the regex.

A soundpack can register hundreds of triggers, and every line the MUD sends is tried against
each of them. Nearly every pattern spells out some fixed text (``^You\\ see\\ (.*?)\\ here\\.``)
that a match has to contain, and looking for that text in the line costs a fraction of running
the regex, more so with the time limit the engine puts on every search. So a line without it is
passed over without asking the regex at all.

It is only ever a shortcut. The text comes from Python's own parse of the pattern, and a
pattern this can't be sure about gets no shortcut and is always run: one Python's parser
won't read (syntax only the regex module knows), case-insensitive matching, or an alternative
at the top level, where no one piece of text is certain.
"""

from __future__ import annotations

import warnings

try:  # Python's own pattern parser; private, so a Python without it just gets no shortcut
    from re import _constants as _nodes  # the kinds of node a parsed pattern is made of
    from re import _parser

    _REPEATS = (_nodes.MAX_REPEAT, _nodes.MIN_REPEAT, _nodes.POSSESSIVE_REPEAT)
except (ImportError, AttributeError):  # pragma: no cover - every supported Python has it
    _parser = None

_IGNORECASE = 2  # the same bit in re and regex
_VERBOSE = 64  # likewise; the parser needs it to skip a verbose pattern's spaces
_REGEX_VERSION1 = 256  # regex's V1, whose sets nest as re's don't (re.ASCII shares the bit)
_MIN_USEFUL = 2  # one character turns up in nearly every line: not worth looking for


def required_text(pattern) -> str | None:
    """The longest run of text every match of ``pattern`` contains, or None to always search.

    ``pattern`` is a compiled pattern from ``re`` or ``regex``. Only text at the top level of
    the pattern counts. The top level is a sequence every match goes through in order, so its
    literal characters are certain; anything else there (a group, which may be optional, a set
    of alternatives, a class, an anchor) ends the run. A character repeated a fixed number of
    times is that many characters; one repeated at least once counts its certain copies and
    ends the run, since more may follow.
    """
    flags = getattr(pattern, "flags", 0)
    source = getattr(pattern, "pattern", None)
    if _parser is None or not isinstance(source, str) or flags & (_IGNORECASE | _REGEX_VERSION1):
        return None
    if _read_otherwise_by_regex(source):
        return None
    try:
        with warnings.catch_warnings():  # "possible set difference" and the like: not ours
            warnings.simplefilter("ignore")
            parsed = _parser.parse(source, flags & _VERBOSE)
    except Exception:  # noqa: BLE001 - syntax only the regex module reads: no shortcut
        return None
    if parsed.state.flags & _IGNORECASE:  # an inline (?i) at the start
        return None
    runs: list[str] = []
    current: list[str] = []
    for kind, value in parsed:
        if kind == _nodes.LITERAL:
            current.append(chr(value))
            continue
        if kind in _REPEATS:
            least, most, item = value
            if least >= 1 and len(item) == 1 and item[0][0] == _nodes.LITERAL:
                current.append(chr(item[0][1]) * least)
                if least == most:
                    continue
        runs.append("".join(current))
        current = []
    runs.append("".join(current))
    longest = max(runs, key=len)
    return longest if len(longest) >= _MIN_USEFUL else None


def _read_otherwise_by_regex(source: str) -> bool:
    """Whether ``source`` holds a brace or a bracket inside a bracket, outside an escape.

    Python's parser reads those as plain text where the regex module may not: ``{e<=1}`` is
    a fuzzy match there, ``[[:alpha:]]`` a class of letters. Text taken from such a pattern
    could be text a match doesn't have, and the trigger would never fire.
    """
    escaped = False
    previous = ""
    for char in source:
        if escaped:
            escaped = False
            previous = ""
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "{" or (previous == "[" and char in "[:"):
            return True
        previous = char
    return False

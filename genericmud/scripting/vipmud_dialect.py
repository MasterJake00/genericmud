"""VIPMud ``.set`` scripting dialect — clean-room interpreter.

VIPMud's language is macro-substitution plus a small command set, documented
publicly (Order of Chaos wiki). It is not a general-purpose language, so a
dedicated interpreter targeting the shared :class:`AutomationEngine` is small and
sufficient. This lets existing VIPMud soundpacks run alongside Lua packs.

Definition commands (load time):
    #TRIGGER <pat> {body}  / #TR <pat> {body}  / #GTRIGGER <pat> {body}  [{} AnyCase]
    #ALIAS <pat> {body}    #KEY <key> {body}
Action commands (fire time, inside a body):
    #SAY {text} [mode]   #PLAY {file} [vol]   #PLAYLOOP {file} [vol]
    #VAR <name> {value}  #GVAR <name> {value}   #STOP
    #IF {cond} {then} [{else}]
    #PC <handle> stop|volume N|pan N|frequency N
    bare text -> sent to the MUD;  bare ``@name = value`` -> variable assignment

Patterns use VIPMud wildcards: ``*`` (any run), ``?`` (one char), and named
wildcards ``&{name}`` / ``&name`` exposed at fire time as ``@name``.
Substitution at fire time: ``@var`` -> variable, ``%0..%9`` -> positional capture,
``%playhandle`` -> the id of the most recently started cue.

Server-controlled packs (e.g. Cosmic Rage) run through this: the MUD streams a
``$sphook play:path:vol:...`` line, the trigger captures the fields as ``@action``
etc., and ``#if`` dispatches to ``#play``/``#playloop``. Unimplemented commands
(``#math``, the ``%function()`` library, ``#wait``, file I/O, ``#Configure``) are
ignored, so a pack loads partially but its sound core works.
"""

from __future__ import annotations

import asyncio
import random
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from genericmud.automation.engine import MatchContext
from genericmud.model.buffer import Line
from genericmud.safepath import confine
from genericmud.scripting.api import ScriptApi

_DEFINITIONS = {"TRIGGER", "TRIG", "TR", "GTRIGGER", "ALIAS", "KEY"}  # VIPMud allows abbreviation
_VAR_RE = re.compile(r"@(\w+)")
_WILDCARD_RE = re.compile(r"%(\d)")
_FORALL_VAR_RE = re.compile(r"%[Ii]\b")  # #ForAll loop variable (%I / %i), expanded per item
_SOUND_VARIANT_RE = re.compile(r"\*(\d+)(\.\w+)$")  # "name*N.ext": one random variant of 1..N
# Named/positional wildcards inside a .set pattern: &{name}, &name, *, ?
_PATTERN_WILDCARD_RE = re.compile(r"&\{(\w+)\}|&(\w+)|(\*)|(\?)")
# A bare "@name = value" statement is an assignment, not text to send.
_ASSIGN_RE = re.compile(r"^@(\w+)\s*=\s*(.*)$")


def _fold(name: str) -> str:
    """Canonicalize a VIPMud variable name: VIPMud is case-insensitive (real packs
    write ``vol`` and read ``@Vol``), but the engine's variable store is a plain dict
    shared with case-sensitive dialects (MUSHclient GetVariable/SetVariable), so the
    folding must happen at this dialect's boundary, on every read and write."""
    return name.lower()
# A single comparison for #if: left <op> right (longer operators first; <> is
# VIPMud's not-equal and packs use it heavily, e.g. every pan/pitch gate in
# Cosmic Rage's $sphook dispatch).
_CONDITION_RE = re.compile(r"^(.*?)\s*(<=|>=|<>|!=|<|>|=)\s*(.*)$")
# Fire-time %function(arg) calls the sound core needs: %var (indirect variable
# read -- how a pack stops a cue whose handle it stored under a server-supplied
# name), %defined (guards those reads), and %ifword (dispatches Star Conquest's
# communicator and other category-specific cues).
_FUNC_RE = re.compile(r"%(var|defined|ifword)\(([^()]*)\)", re.IGNORECASE)
# #math accepts plain arithmetic once variables are substituted; anything else
# (names, quotes) is refused rather than guessed at. The character class allows `*`, hence `**`,
# so exponentiation is rejected separately below -- `9**9**9` is regex-clean but a big-int CPU/
# memory bomb (and a hostile MUD can feed the operands via a captured variable).
_MATH_SAFE_RE = re.compile(r"^[\d\s.+\-*/()]+$")
_MATH_MAX_LEN = 256  # a pan/level calc is tiny; refuse anything pathologically long
_DEFAULT_VOLUME = 100
_MASTER_HANDLE = "0"  # VIPMud handle 0 == all cues / master volume
_MAX_EXEC_DEPTH = 20  # bare-text alias expansion recursion cap (self-invoking alias)
_PLAY_HANDLE_TOKEN = "%playhandle"
# VIPMud key names ("Alt-Shift-PageUp", "control-f11") -> the combo the window builds
# (wx_app._key_combo: ctrl, alt, shift in that order, then the key, joined with "+").
_KEY_MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift"}
_KEY_MODIFIER_ORDER = ("ctrl", "alt", "shift")
_KEY_NAMES = {
    "pgup": "pageup", "pgdn": "pagedown", "del": "delete", "ins": "insert",
    "return": "enter", "esc": "escape", "spacebar": "space",
}
_REMOTE_SOUND_MAX_BYTES = 32 * 1024 * 1024
# A sound that failed to download is left alone this long before a cue tries it again, so a
# file the server lacks isn't fetched on every cue but one timeout doesn't silence it for good.
_FETCH_RETRY_SECONDS = 300.0


@dataclass
class _Tok:
    braced: bool
    text: str


def _scan_block(source: str, i: int) -> tuple[str, int]:
    """``source[i]`` is ``{``; return (inner text, index after matching ``}``)."""
    depth = 0
    start = i + 1
    j = i
    n = len(source)
    while j < n:
        c = source[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return source[start:j], j + 1
        j += 1
    return source[start:], n  # unbalanced: take the rest


def tokenize_statements(source: str) -> list[tuple[str | None, list[_Tok]]]:
    """Split a .set source (or a command body) into statements.

    Statements are newline/``;`` separated, but a ``{...}`` block may span
    newlines. ``//`` begins a line comment. A statement is ``(command, args)``;
    ``command`` is ``None`` for a bare line of text to send.
    """
    statements: list[tuple[str | None, list[_Tok]]] = []
    i = 0
    n = len(source)
    while i < n:
        while i < n:
            c = source[i]
            if c in " \t\r\n":
                i += 1
            elif c == ";":
                # A ';' with nothing but whitespace before it on its line begins a
                # comment (VIPMud .set convention, ";core variables"); consumed to EOL.
                # Mid-line it is the statement separator, skipped like whitespace --
                # without the distinction, comment text was SENT to the MUD at load.
                k = i - 1
                while k >= 0 and source[k] in " \t":
                    k -= 1
                if k < 0 or source[k] in "\r\n":
                    while i < n and source[i] != "\n":
                        i += 1
                else:
                    i += 1
            else:
                break
        if i >= n:
            break
        if source.startswith("//", i):
            while i < n and source[i] != "\n":
                i += 1
            continue
        if source[i] == "#":
            i += 1
            start = i
            while i < n and source[i] not in " \t\r\n;{":  # ; ends a bare command (e.g. #stop;)
                i += 1
            command = source[start:i].upper()
            args: list[_Tok] = []
            while i < n:
                while i < n and source[i] in " \t":
                    i += 1
                if i >= n or source[i] in "\r\n;":  # ; ends a statement (same as newline)
                    break
                if source[i] == "{":
                    inner, i = _scan_block(source, i)
                    args.append(_Tok(True, inner))
                else:
                    word_start = i
                    while i < n and source[i] not in " \t\r\n;{":
                        i += 1
                    args.append(_Tok(False, source[word_start:i]))
            statements.append((command, args))
        else:
            line_start = i
            # ';' separates statements in bare text too ("makebetter; look" is two
            # commands); without the split the whole line went out as one string and
            # a leading alias name prefix-matched, swallowing the rest.
            while i < n and source[i] not in "\r\n;":
                i += 1
            text = source[line_start:i].strip()
            if text:
                statements.append((None, [_Tok(False, text)]))
    return statements


def _scan_char_class(pattern: str, start: int) -> int | None:
    """End index (exclusive) of the ``[...]`` class opening at ``start``, or None if unclosed.

    Follows the regex rule that a ``]`` in the first position (after an optional ``^``) is a
    member rather than the terminator. That rule is the whole point here: it is how packs
    write a literal bracket, ``[[]`` for ``[`` and ``[]]`` for ``]``.
    """
    i = start + 1
    if i < len(pattern) and pattern[i] == "^":
        i += 1
    if i < len(pattern) and pattern[i] == "]":
        i += 1
    while i < len(pattern):
        if pattern[i] == "]":
            return i + 1
        i += 1
    return None


def _char_class(source: str) -> str:
    """Normalise a ``[...]`` group into an equivalent regex character class.

    Bracket members are re-escaped, so ``[[]`` becomes ``[\\[]``: identical in meaning, but
    it keeps Python's "possible nested set" FutureWarning out of the user's console every
    time the pack loads. Falls back to escaping the whole group as a literal if the result
    still will not compile, so a malformed class can never kill a pack load.
    """
    body = source[1:-1]
    negated = body.startswith("^")
    if negated:
        body = body[1:]
    members: list[str] = []
    i = 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            members.append(body[i : i + 2])  # an existing escape passes through intact
            i += 2
            continue
        members.append("\\" + body[i] if body[i] in "[]" else body[i])
        i += 1
    candidate = f"[{'^' if negated else ''}{''.join(members)}]"
    try:
        re.compile(candidate)
    except re.error:
        return re.escape(source)
    return candidate


def vip_pattern_to_regex(pattern: str) -> str:
    """Convert a VIPMud wildcard pattern to a regex with named/numbered captures.

    ``&{name}``/``&name`` -> a named group (read at fire time as ``@name``); ``*`` ->
    a numbered group; ``?`` -> one char. A wildcard at the very end takes the rest
    (greedy); a bounded one is non-greedy so the following literal delimits it.

    ``[...]`` is a character class and passes through, because that is what VIPMud does and
    packs depend on it: channel triggers are written ``[[]*] * *`` to match a line like
    ``[General Communication] Someone transmits, "..."``. Escaping the brackets as literals
    instead produced a regex demanding three characters, ``[[]``, and every channel trigger
    in the Star Conquest pack silently matched nothing.
    """
    out: list[str] = []
    literal_start = 0
    i = 0
    end = len(pattern)
    while i < end:
        if pattern[i] == "[":
            close = _scan_char_class(pattern, i)
            if close is None:  # unclosed: an ordinary literal bracket
                i += 1
                continue
            out.append(re.escape(pattern[literal_start:i]))
            out.append(_char_class(pattern[i:close]))
            i = literal_start = close
            continue
        match = _PATTERN_WILDCARD_RE.match(pattern, i)
        if match is None:
            i += 1
            continue
        out.append(re.escape(pattern[literal_start : match.start()]))
        body = ".*" if match.end() == end else ".*?"
        name = match.group(1) or match.group(2)
        if name:
            out.append(f"(?P<{name}>{body})")
        elif match.group(3):  # *
            out.append(f"({body})")
        else:  # ?
            out.append("(.)")
        i = literal_start = match.end()
    out.append(re.escape(pattern[literal_start:]))
    return "".join(out)


def vip_key_to_combo(spec: str) -> str:
    """A VIPMud #key name as the combo the window sends for that keypress.

    Packs write "alt-f2", "Control-alt-shift-S" and "Shift-Alt-PageUp"; the window sends
    "alt+f2", "ctrl+alt+shift+s" and "alt+shift+pageup". Taken as written, every
    hotkey with a modifier could never fire: 124 of Cosmic Rage's 148, 32 of Star
    Conquest's 40. The key itself may be "-" ("alt--"), so modifiers are peeled off
    the front one at a time rather than split on every dash.
    """
    remaining = "".join(spec.split()).lower()  # "shift-alt + pgdn" spacing varies too
    modifiers: set[str] = set()
    peeled = True
    while peeled:
        peeled = False
        for word, modifier in _KEY_MODIFIERS.items():
            for separator in "-+":
                prefix = word + separator
                if remaining.startswith(prefix) and len(remaining) > len(prefix):
                    modifiers.add(modifier)
                    remaining = remaining[len(prefix):]
                    peeled = True
                    break
            if peeled:
                break
    key = _KEY_NAMES.get(remaining, remaining)
    ordered = [modifier for modifier in _KEY_MODIFIER_ORDER if modifier in modifiers]
    return "+".join([*ordered, key])


def _to_int(value: str, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _split_function_args(text: str) -> list[str]:
    """Split a VIPMud function's comma-separated arguments, preserving quoted commas."""
    args: list[str] = []
    start = 0
    quote = ""
    for i, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == ",":
            args.append(text[start:i].strip())
            start = i + 1
    args.append(text[start:].strip())
    return args


def _strip_outer_parentheses(expression: str) -> str:
    """Remove parentheses only when they enclose the complete expression."""
    expression = expression.strip()
    while len(expression) >= 2 and expression[0] == "(" and expression[-1] == ")":
        depth = 0
        quote = ""
        encloses_all = True
        for i, char in enumerate(expression):
            if quote:
                if char == quote:
                    quote = ""
                continue
            if char in "\"'":
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and i != len(expression) - 1:
                    encloses_all = False
                    break
                if depth < 0:
                    encloses_all = False
                    break
        if not encloses_all or depth != 0:
            break
        expression = expression[1:-1].strip()
    return expression


def _split_boolean(expression: str, operator: str) -> list[str]:
    """Split a top-level ``and``/``or``, ignoring quoted text and parenthesized groups."""
    parts: list[str] = []
    start = 0
    depth = 0
    quote = ""
    lower = expression.lower()
    i = 0
    while i < len(expression):
        char = expression[i]
        if quote:
            if char == quote:
                quote = ""
            i += 1
            continue
        if char in "\"'":
            quote = char
            i += 1
            continue
        if char == "(":
            depth += 1
            i += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        end = i + len(operator)
        before_word = i > 0 and (expression[i - 1].isalnum() or expression[i - 1] == "_")
        after_word = end < len(expression) and (
            expression[end].isalnum() or expression[end] == "_"
        )
        if depth == 0 and lower[i:end] == operator and not before_word and not after_word:
            parts.append(expression[start:i].strip())
            start = end
            i = end
            continue
        i += 1
    if parts:
        parts.append(expression[start:].strip())
        return parts
    return [expression]


def _as_number(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _expand_sound_variant(path: str) -> str:
    """VIPMud plays a random variant of ``name*N.ext`` — one of ``name1.ext``..``nameN.ext``.

    Expand to a concrete variant chosen at random (the source of the pack's sound variety);
    a path without the ``*N`` marker passes through unchanged.
    """
    match = _SOUND_VARIANT_RE.search(path)
    if not match:
        return path
    count = int(match.group(1))
    pick = random.randint(1, count) if count >= 1 else 1
    return path[: match.start()] + str(pick) + match.group(2)


_SETTINGS_RECORD = 128  # VIPMud settings files store one value per fixed 128-byte record


def _parse_vip_settings(data: bytes) -> list[str]:
    """Decode a VIPMud settings file into its values, one per record.

    The format is fixed 128-byte records, each a uint16 little-endian length then that many
    ASCII bytes, null-padded. Reading it (via #file/#Read) is what flips a pack's sound-enable
    flags on — they otherwise keep the conservative defaults set before the read.
    """
    values: list[str] = []
    for off in range(0, len(data), _SETTINGS_RECORD):
        record = data[off : off + _SETTINGS_RECORD]
        if len(record) < 2:
            break
        length = int.from_bytes(record[0:2], "little")
        values.append(record[2 : 2 + length].decode("latin-1", "ignore"))
    return values


def _serialize_vip_settings(values: list[str]) -> bytes:
    """Re-encode values into the 128-byte-record format (for #Write -> #Close persistence)."""
    out = bytearray()
    for value in values:
        encoded = value.encode("latin-1", "ignore")[: _SETTINGS_RECORD - 2]
        out += (len(encoded).to_bytes(2, "little") + encoded).ljust(_SETTINGS_RECORD, b"\x00")
    return bytes(out)


@dataclass
class _PendingCue:
    """A cue waiting for its sound to download; #pc volume and pan land here until it plays."""

    file: str
    gain: float
    loop: bool
    pan: float = 0.0


@dataclass
class _VipFile:
    path: Path | None  # where to flush on #Close (None if unresolved)
    values: list[str]  # one entry per record, addressed 1-based by #Read/#Write
    dirty: bool = False


class _Abort(Exception):  # noqa: N818 - a control-flow signal (#Abort), not an error condition
    """Raised by ``#Abort`` to stop the current script body (a trigger fire or a load)."""


class VipMudPack:
    """A loaded VIPMud soundpack/script, registered against a :class:`ScriptApi`."""

    def __init__(self, api: ScriptApi) -> None:
        self._api = api
        self._base_dir = api.base_dir
        self._loaded: set[str] = set()  # files already #load-ed (cycle/dup guard)
        self._files: dict[str, _VipFile] = {}  # #file handle -> open settings file
        self._handles: dict[str, str] = {}  # VIPMud play handle -> sound bus channel
        self._next_handle = 1
        self._exec_depth = 0  # bare-text -> alias-pipeline recursion guard
        self._last_handle = _MASTER_HANDLE
        self._remote_sound_base: str | None = None
        self._remote_sound_root: Path | None = None
        # Sounds fetched in the background (see _fetch_in_background), by URL: the download
        # in flight, the cue channels waiting on it, and URLs that already failed this session.
        self._fetching: dict[str, asyncio.Future] = {}
        self._waiting: dict[str, list[str]] = {}
        self._pending_cues: dict[str, _PendingCue] = {}  # channel -> cue waiting on a download
        self._failed_fetches: dict[str, float] = {}  # url -> when it failed (time.monotonic)
        # Server-controlled packs build sound paths from @sppath; the pack's own
        # settings loader (file I/O, deferred) normally sets it. Default it (and the
        # script path) to the pack dir so @sppath/x.wav resolves to bundled sounds.
        # Default @sppath/@scpath to the pack dir, but don't clobber a value the session
        # pre-set from world.sounds (so a world can point at its own sound folder).
        base = api.base_dir
        if base:
            self._discover_remote_sounds(Path(base))
        if base and not api.get_var("sppath"):
            api.set_var("sppath", self._remote_sound_root or base)
        if base and not api.get_var("scpath"):
            api.set_var("scpath", base)

    def _discover_remote_sounds(self, base: Path) -> None:
        """Read an official VIPMud installer's lazy sound-repository declaration."""
        installers = sorted(base.rglob("installer.bat"))
        for installer in installers:
            try:
                source = installer.read_text(encoding="latin-1", errors="ignore")
            except OSError:
                continue
            repo = re.search(r'set\s+"REPO_URL=([^"]+)"', source, re.IGNORECASE)
            subfolder = re.search(
                r'set\s+"REPO_SUBFOLDER=([^"]+)"', source, re.IGNORECASE
            )
            helper = installer.parent / "SoundSync.exe"
            if repo is None or subfolder is None or not helper.is_file():
                continue
            repo_url = re.sub(r"(?<!:)//+", "/", repo.group(1).strip()).rstrip("/")
            subtree = subfolder.group(1).strip().strip("/\\")
            if not repo_url.startswith(("http://", "https://")) or not subtree:
                continue
            self._remote_sound_base = f"{repo_url}/raw/branch/main/{quote(subtree)}/"
            self._remote_sound_root = installer.parent / "sounds" / subtree
            return

    def _remote_sound_request(self, file: str) -> tuple[Path, str] | None:
        root = self._remote_sound_root
        if root is None or self._remote_sound_base is None:
            return None
        requested = Path(file.replace("\\", "/"))
        candidate = requested if requested.is_absolute() else root / requested
        try:
            destination = candidate.resolve()
            root = root.resolve()
        except OSError:
            return None
        if not destination.is_relative_to(root) or destination.suffix.casefold() != ".wav":
            return None
        relative = destination.relative_to(root).as_posix()
        url = self._remote_sound_base + quote(relative, safe="/")
        return destination, url

    def can_fetch_sound(self, file: str) -> bool:
        """Whether this pack declares a confined online source for the requested WAV."""
        return self._remote_sound_request(file) is not None

    def _fetch_remote_sound(self, file: str) -> bool:
        request = self._remote_sound_request(file)
        if request is None:
            return False
        destination, url = request
        root = self._remote_sound_root
        assert root is not None
        relative = destination.relative_to(root.resolve()).as_posix()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, prefix=".genericmud-sound-", delete=False
            ) as handle:
                temporary = Path(handle.name)
            from genericmud.packs import vault

            vault.download(url, temporary, max_bytes=_REMOTE_SOUND_MAX_BYTES)
            if temporary.stat().st_size == 0:
                return False
            temporary.replace(destination)
            if self._api.diag is not None:
                self._api.diag.event(
                    "pack.sound.fetch", source=self._api.source or "?",
                    file=relative, status="downloaded",
                )
            return True
        except Exception as error:  # noqa: BLE001 - the pack handles a failed play normally
            if self._api.diag is not None:
                self._api.diag.event(
                    "pack.sound.fetch", source=self._api.source or "?",
                    file=relative, status="failed", error=f"{type(error).__name__}: {error}",
                )
            return False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def load_source(self, source: str) -> None:
        for command, args in tokenize_statements(source):
            if command in _DEFINITIONS:
                self._define(command, args)
            else:
                try:
                    self._execute_statement(command, args, [])
                except _Abort:
                    return  # a top-level #Abort stops loading the rest of this script

    def _define(self, command: str, args: list[_Tok]) -> None:
        if not args:
            return
        body = args[1].text if len(args) > 1 else ""  # {body} is the first block after the pattern
        if command in ("TRIGGER", "TRIG", "TR", "GTRIGGER"):
            flags = "(?i)" if any(a.text.lower() == "anycase" for a in args[2:]) else ""
            self._api.add_trigger(
                flags + vip_pattern_to_regex(args[0].text), self._runner(body), regex=True
            )
        elif command == "ALIAS":
            self._define_alias(args[0].text, body)
        elif command == "KEY":
            self._api.add_key(vip_key_to_combo(args[0].text), self._runner(body))

    def _define_alias(self, name: str, body: str) -> None:
        """``#ALIAS``: the first word of what the player types names the alias.

        Real packs are written that way: ``#alias {dk} {dirtkick %1}`` expects "dk goblin" to
        dirtkick the goblin, and Cogg's ``#Alias {reply} {tell @tellname %0}`` expects %0 to be
        the whole message. So the alias matches only when its name is the first word, ``%0`` is
        everything after it and ``%1``..``%9`` are those words one at a time. Matching a bare
        prefix instead let Prometheus's one-letter fly-mode aliases ("s", "n", "e") swallow
        "say", "north" and "exits", and dropped every argument. VIPMud is case-insensitive
        elsewhere (variable names), so alias names are too. A name that carries wildcards
        ("gt *") is a pattern instead, and has to match the whole command.
        """
        if _PATTERN_WILDCARD_RE.search(name) or name.split() != [name]:
            pattern = f"(?i)^{vip_pattern_to_regex(name)}$"
            self._api.add_alias(pattern, self._runner(body), regex=True)
            return
        pattern = rf"(?i)^{re.escape(name)}(?:\s+(.*?))?\s*$"
        self._api.add_alias(pattern, self._alias_runner(body), regex=True)

    def _alias_runner(self, body: str):
        def run(ctx: MatchContext) -> None:
            parameters = ctx.wildcards[1] if len(ctx.wildcards) > 1 else ""
            # %0 is everything typed after the alias name; %1.. are its words in turn.
            wildcards = [parameters, *parameters.split()]
            try:
                self._execute_body(body, wildcards, line=ctx.line)
            except _Abort:
                pass  # #Abort: stop this alias body early, leaving prior effects in place

        return run

    def _runner(self, body: str):
        def run(ctx: MatchContext) -> None:
            for name, value in ctx.named.items():  # named wildcards become @vars
                self._api.set_var(_fold(name), value or "")
            try:
                self._execute_body(body, ctx.wildcards, line=ctx.line)
            except _Abort:
                pass  # #Abort: stop this trigger body early, leaving prior effects in place

        return run

    def _execute_body(self, body: str, wildcards: list[str], line: Line | None = None) -> None:
        for command, args in tokenize_statements(body):
            self._execute_statement(command, args, wildcards, line)

    def _execute_statement(
        self, command: str | None, args: list[_Tok], wildcards: list[str], line: Line | None = None
    ) -> None:
        if command is None or command == "SEND":
            self._bare_or_send(args, wildcards)
        elif command == "SAY" and args:
            self._api.speak(self._subst(args[0].text, wildcards))
        elif command in ("PLAY", "PLAYLOOP") and args:
            self._play(args, wildcards, loop=command == "PLAYLOOP")
        elif command == "VAR" and len(args) > 1:
            name = _fold(self._subst(args[0].text, wildcards))  # @-indirect target supported
            self._api.set_var(name, self._subst(args[1].text, wildcards))
        elif command == "GVAR" and len(args) > 1:  # global namespace; @-reads still resolve it
            name = _fold(self._subst(args[0].text, wildcards))
            self._api.set_gvar(name, self._subst(args[1].text, wildcards))
        elif command == "UNVAR" and args:
            self._api.delete_var(_fold(self._subst(args[0].text, wildcards)))
        elif command == "MATH" and len(args) > 1:
            self._execute_math(args, wildcards)
        elif command == "STOP":
            # VIPMud #Stop halts every playing sound. The api default channel ("sound")
            # is one this dialect never plays on -- every cue goes to a per-cue vip-N
            # channel -- so a plain stop() was a no-op against our own loops.
            self._pending_cues.clear()
            self._api.flush()
        elif command == "IF" and args:
            self._execute_if(args, wildcards, line)
        elif command == "FORALL" and len(args) > 1:
            self._execute_forall(args, wildcards, line)
        elif command == "ALARM" and len(args) > 1:
            self._execute_alarm(args, wildcards)
        elif command == "GAGLINE" and line is not None:
            # "#gagline [count] voice" gags self-voice but keeps the line reviewable; without a
            # "voice" arg ("all", a bare count, or nothing) it removes the line entirely. Packs
            # play a sound in the gagged line's place. (SC writes "voice"; Prometheus "1 Voice".)
            line.gagged = True
            line.display_when_gagged = any(a.text.lower() == "voice" for a in args)
        elif command == "ABORT":
            raise _Abort
        elif command == "PC" and len(args) > 1:
            self._execute_pc(args, wildcards)
        elif command == "LOAD" and args:
            self._load_file(self._subst(args[0].text, wildcards))
        elif command == "FILE" and len(args) > 1:
            self._open_file(args, wildcards)
        elif command == "READ" and len(args) > 2:
            self._read_file(args, wildcards)
        elif command == "WRITE" and len(args) > 2:
            self._write_file(args, wildcards)
        elif command == "CLOSE" and args:
            self._close_file(args, wildcards)
        # Unknown #commands are ignored rather than sent to the MUD (Phase 2 features).

    def _resolve_in_pack(self, reference: str, *, must_exist: bool) -> Path | None:
        """Resolve a pack-relative path: an exact file under the pack dir, else the first match
        by basename (case-insensitive — Windows-authored packs are careless about case and the
        in-pack layout rarely matches a script's hardcoded path). When ``must_exist`` is False
        and nothing matches, return ``base/target`` so a new file (e.g. saved settings) can be
        created there. Always confined to the pack dir (rglob can surface escaping symlinks)."""
        if not self._base_dir:
            return None
        base = Path(self._base_dir).resolve()
        target = Path(reference.replace("\\", "/"))
        if not target.name:
            return None
        candidate = target if target.is_absolute() else base / target
        try:
            real = candidate.resolve()
        except OSError:
            real = None
        if real is not None and real.is_file() and real.is_relative_to(base):
            return real
        wanted = target.name.lower()
        for match in sorted(base.rglob("*")):
            if match.name.lower() != wanted:
                continue
            try:
                resolved = match.resolve()
            except OSError:
                continue
            if resolved.is_file() and resolved.is_relative_to(base):
                return resolved
        if must_exist:
            return None
        # Creating a new file (a settings file #Write will flush): confine it strictly to the
        # pack dir. Refuse an absolute/UNC/.. reference or anything resolving outside the pack,
        # so #file/#write can't create or overwrite a file elsewhere on disk.
        return confine(base, reference)

    def _load_file(self, reference: str) -> None:
        """``#LOAD {file}`` — load another ``.set`` from the pack so a loader script
        pulls in all the pack's scripts. Confined to the pack dir; each file loads once."""
        real = self._resolve_in_pack(reference, must_exist=True)
        if real is None:
            return
        key = str(real)
        if key in self._loaded:  # don't re-load (handles #load cycles + duplicates)
            return
        self._loaded.add(key)
        self.load_source(real.read_text(encoding="latin-1", errors="ignore"))

    def _open_file(self, args: list[_Tok], wildcards: list[str]) -> None:
        """``#file <handle> {path} [mode]`` — open a VIPMud settings file under a handle so
        #Read/#Write address it by record number. Reading the pack's shipped settings is what
        turns its sound-enable flags on (e.g. Star Conquest gates every #Play on @silent=1, which
        lives in Settings.set); without this they keep the off-by-default set before the read."""
        handle = self._subst(args[0].text, wildcards)
        path = self._resolve_in_pack(self._subst(args[1].text, wildcards), must_exist=False)
        values: list[str] = []
        if path is not None and path.is_file():
            try:
                values = _parse_vip_settings(path.read_bytes())
            except OSError:
                values = []
        self._files[handle] = _VipFile(path=path, values=values)

    def _read_file(self, args: list[_Tok], wildcards: list[str]) -> None:
        """``#Read <handle> <var> <record>`` — read a 1-based record into the variable."""
        vfile = self._files.get(self._subst(args[0].text, wildcards))
        if vfile is None:
            return
        index = _to_int(self._subst(args[2].text, wildcards), 0) - 1
        if 0 <= index < len(vfile.values):
            self._api.set_var(_fold(args[1].text), vfile.values[index])

    def _write_file(self, args: list[_Tok], wildcards: list[str]) -> None:
        """``#Write <handle> <value> <record>`` — set a 1-based record (flushed on #Close)."""
        vfile = self._files.get(self._subst(args[0].text, wildcards))
        if vfile is None:
            return
        index = _to_int(self._subst(args[2].text, wildcards), 0) - 1
        if index < 0:
            return
        while len(vfile.values) <= index:
            vfile.values.append("")
        vfile.values[index] = self._subst(args[1].text, wildcards)
        vfile.dirty = True

    def _close_file(self, args: list[_Tok], wildcards: list[str]) -> None:
        """``#Close <handle>`` — flush a written settings file back to disk, then drop it."""
        vfile = self._files.pop(self._subst(args[0].text, wildcards), None)
        if vfile is not None and vfile.dirty and vfile.path is not None:
            try:
                vfile.path.parent.mkdir(parents=True, exist_ok=True)
                vfile.path.write_bytes(_serialize_vip_settings(vfile.values))
            except OSError:
                pass

    def _bare_or_send(self, args: list[_Tok], wildcards: list[str]) -> None:
        if not args:
            return
        text = args[-1].text
        assignment = _ASSIGN_RE.match(text)
        if assignment:  # "@name = value" sets a variable; it isn't sent to the MUD
            self._api.set_var(
                _fold(assignment.group(1)), self._subst(assignment.group(2), wildcards)
            )
            return
        # Bare text runs "as if typed": VIPMud expands aliases in script bodies (packs
        # call their own aliases by name, e.g. CR's `makebetter`), so route through the
        # alias pipeline; unmatched text still reaches the wire. Depth-capped: an alias
        # whose body re-invokes itself would otherwise recurse without bound.
        if self._exec_depth >= _MAX_EXEC_DEPTH:
            return
        self._exec_depth += 1
        try:
            self._api.execute(self._subst(text, wildcards))
        finally:
            self._exec_depth -= 1

    def _play(self, args: list[_Tok], wildcards: list[str], *, loop: bool) -> None:
        file = _expand_sound_variant(self._subst(args[0].text, wildcards))
        volume = _DEFAULT_VOLUME
        if len(args) > 1:
            volume = _to_int(self._subst(args[1].text, wildcards), _DEFAULT_VOLUME)
        handle = str(self._next_handle)
        self._next_handle += 1
        channel = f"vip-{handle}"  # one channel per cue so #pc can target it later
        self._handles[handle] = channel
        if not self._api.sound_exists(file) and self._fetch_in_background(
            file, channel, volume, loop
        ):
            self._last_handle = handle  # it will play once the file lands
            return
        played = self._api.play(file, channel=channel, gain=volume / 100, loop=loop)
        # VIPMud reports a failed play as handle 0, and packs branch on it -- Cosmic
        # Rage speaks the failure and auto-downloads the missing file.
        self._last_handle = handle if played else "0"

    def _fetch_in_background(self, file: str, channel: str, volume: int, loop: bool) -> bool:
        """Start (or join) a download of a missing sound; True if the cue will play later.

        The pack's installer names an online sound tree (Cosmic Rage's VIPMud pack: several GB
        on its gitea), so a missing .wav is fetched the first time a cue wants it. That used to
        happen right here, inside line processing, on the event loop every tab and all speech
        share, with no timeout: each new sound froze the client while it downloaded, a
        stalled server froze it for good, and a file the server lacked was fetched again on
        every cue. Now the download runs on a worker thread and the cue plays when it lands,
        unless #pc stopped it first; a URL that failed isn't tried again this session.
        """
        request = self._remote_sound_request(file)
        if request is None:
            return False
        _destination, url = request
        failed_at = self._failed_fetches.get(url)
        if failed_at is not None and time.monotonic() - failed_at < _FETCH_RETRY_SECONDS:
            return False  # play() then reports the miss as handle 0, which packs handle
        try:
            loop_ = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (a headless load): fetch in place, as before, and play straight away.
            if not self._fetch_remote_sound(file):
                self._failed_fetches[url] = time.monotonic()
            return False
        self._pending_cues[channel] = _PendingCue(file, gain=volume / 100, loop=loop)
        self._waiting.setdefault(url, []).append(channel)
        if url not in self._fetching:
            future = loop_.run_in_executor(None, self._fetch_remote_sound, file)
            self._fetching[url] = future
            self._api.track_pending(future)  # closing the session cancels the late play
            future.add_done_callback(lambda done, url=url: self._fetched(url, done))
        return True

    def _fetched(self, url: str, future: asyncio.Future) -> None:
        """A background download finished (on the event loop): play the cues still waiting."""
        self._fetching.pop(url, None)
        self._api.forget_pending(future)
        channels = self._waiting.pop(url, [])
        landed = not future.cancelled() and future.exception() is None and bool(future.result())
        if not landed:
            if not future.cancelled():
                self._failed_fetches[url] = time.monotonic()
            for channel in channels:
                self._pending_cues.pop(channel, None)
            return
        for channel in channels:
            cue = self._pending_cues.pop(channel, None)
            if cue is None:
                continue  # stopped while it was downloading
            self._api.play(cue.file, channel=channel, gain=cue.gain, pan=cue.pan, loop=cue.loop)

    def _execute_if(self, args: list[_Tok], wildcards: list[str], line: Line | None = None) -> None:
        branch = 1 if self._eval_condition(args[0].text, wildcards) else 2  # then : else
        if len(args) > branch:
            self._execute_body(args[branch].text, wildcards, line)

    def _execute_forall(
        self, args: list[_Tok], wildcards: list[str], line: Line | None = None
    ) -> None:
        """``#ForAll {a|b|c} {body}`` — run ``body`` once per ``|``-separated item with the loop
        token ``%I`` replaced by the item. VIPMud loaders use it to pull in every script, e.g.
        ``#ForAll {combat|ground|...} {#load {Scripts\\%I.set}}``."""
        body = args[1].text
        for item in self._subst(args[0].text, wildcards).split("|"):
            item = item.strip()
            if item:
                expanded = _FORALL_VAR_RE.sub(lambda _m, it=item: it, body)
                self._execute_body(expanded, wildcards, line)

    def _execute_alarm(self, args: list[_Tok], wildcards: list[str]) -> None:
        """``#alarm <delay> {body}`` — run ``body`` after ``delay`` seconds (a VIPMud timer).

        Packs defer loading the rest of the pack until the login line arrives, via
        ``#alarm 0 {#load ...}`` fired from a login trigger. Cancel/named forms (``#alarm -1``,
        ``#unalarm``) aren't modelled. Fires only when an event loop drives the engine's
        scheduler (the live app); under a no-op scheduler the timer simply never runs.
        """
        delay = _as_number(self._subst(args[0].text, wildcards))
        if delay is None or delay < 0:
            return
        body = args[1].text
        self._api.add_timer(delay, lambda: self._execute_body(body, []))

    def _execute_math(self, args: list[_Tok], wildcards: list[str]) -> None:
        """``#math <var> {expr}`` — arithmetic into a variable. Cosmic Rage computes its
        stereo pan with it (``#math pan {@pan * 50}``), so without it every directional
        cue collapsed to center. Only plain arithmetic survives substitution; anything
        with residual names is refused (never eval'd)."""
        expression = self._subst(args[1].text, wildcards)
        clean = _MATH_SAFE_RE.match(expression) and "**" not in expression
        if not clean or len(expression) > _MATH_MAX_LEN:
            return  # reject names, exponentiation (a big-int bomb), and over-long expressions
        try:
            result = eval(expression, {"__builtins__": {}}, {})  # noqa: S307 - regex-gated, no **
        except (SyntaxError, ZeroDivisionError, ValueError, TypeError):
            return
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        self._api.set_var(_fold(self._subst(args[0].text, wildcards)), result)

    def _eval_condition(self, condition: str, wildcards: list[str]) -> bool:
        return self._eval_expression(self._subst(condition, wildcards))

    def _eval_expression(self, expression: str) -> bool:
        expression = _strip_outer_parentheses(expression)
        alternatives = _split_boolean(expression, "or")
        if len(alternatives) > 1:
            return any(self._eval_expression(part) for part in alternatives)
        requirements = _split_boolean(expression, "and")
        if len(requirements) > 1:
            return all(self._eval_expression(part) for part in requirements)
        negated = re.match(r"(?i)^not\b(.*)$", expression)
        if negated:
            return not self._eval_expression(negated.group(1))
        match = _CONDITION_RE.match(expression)
        if not match:
            number = _as_number(_unquote(expression))
            return number is not None and number != 0
        left, op, right = _unquote(match.group(1)), match.group(2), _unquote(match.group(3))
        if op == "=":
            return left.lower() == right.lower()
        if op in ("<>", "!="):
            return left.lower() != right.lower()
        left_n, right_n = _as_number(left), _as_number(right)
        if left_n is None or right_n is None:
            return False
        return {"<": left_n < right_n, ">": left_n > right_n,
                "<=": left_n <= right_n, ">=": left_n >= right_n}[op]

    def _execute_pc(self, args: list[_Tok], wildcards: list[str]) -> None:
        handle = self._subst(args[0].text, wildcards).strip()
        action = args[1].text.lower()
        value = self._subst(args[2].text, wildcards) if len(args) > 2 else ""
        if handle == _MASTER_HANDLE:  # 0 controls all cues / the master gain
            if action == "stop":
                self._pending_cues.clear()
                self._api.flush()
            elif action == "volume":
                self._api.set_master(_to_int(value, _DEFAULT_VOLUME) / 100)
            return
        channel = self._handles.get(handle)
        if channel is None:
            return
        if action == "stop":
            self._pending_cues.pop(channel, None)  # still downloading: don't start it later
            self._api.stop(channel)
        elif action == "volume":
            # Live change on the playing cue (a per-cue channel's future is one cue, so
            # the old category-policy set_volume adjusted nothing anyone would hear).
            gain = _to_int(value, _DEFAULT_VOLUME) / 100
            if channel in self._pending_cues:
                self._pending_cues[channel].gain = gain  # still downloading: start at this
            else:
                self._api.adjust(channel, gain=gain)
        elif action == "pan":
            # VIPMud pan is -100..100; the bus wants -1..1.
            pan = max(-1.0, min(1.0, _to_int(value, 0) / 100))
            if channel in self._pending_cues:
                self._pending_cues[channel].pan = pan
            else:
                self._api.adjust(channel, pan=pan)
        # frequency: no live pitch control in the bus — accepted, ignored.

    def _subst(self, text: str, wildcards: list[str]) -> str:
        text = _VAR_RE.sub(lambda m: self._api.get_var(_fold(m.group(1))), text)
        text = text.replace(_PLAY_HANDLE_TOKEN, self._last_handle)
        # Functions run after @vars so %var(@id) reads the variable @id names. Positional
        # wildcards are expanded per argument inside _call_function, before %ifword compares
        # them, then once more over the remaining text.
        text = _FUNC_RE.sub(lambda match: self._call_function(match, wildcards), text)
        return self._subst_wildcards(text, wildcards)

    @staticmethod
    def _subst_wildcards(text: str, wildcards: list[str]) -> str:
        def wildcard(match: re.Match[str]) -> str:
            index = int(match.group(1))
            return wildcards[index] if index < len(wildcards) else ""

        return _WILDCARD_RE.sub(wildcard, text)

    def _call_function(self, match: re.Match[str], wildcards: list[str]) -> str:
        name = match.group(1).lower()
        args = [
            _unquote(self._subst_wildcards(arg, wildcards))
            for arg in _split_function_args(match.group(2))
        ]
        if name == "ifword":
            if len(args) < 2 or not args[0]:
                return "0"
            needle, words = args[0].casefold(), args[1].casefold()
            delimiter = args[2].casefold() if len(args) > 2 else " "
            if not delimiter:
                return "1" if needle == words else "0"
            return "1" if f"{delimiter}{needle}{delimiter}" in (
                f"{delimiter}{words}{delimiter}"
            ) else "0"
        arg = args[0] if args else ""
        value = self._api.get_var(_fold(arg), None)
        if name == "defined":
            return "1" if value is not None else "0"
        return value if value is not None else ""  # %var of an unset name reads empty

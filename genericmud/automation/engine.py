"""Automation engine: triggers, aliases, keybindings, timers, variables, gags.

This is the dialect-agnostic core. Every scripting front-end (native Lua, the
MUSHclient compat shim, the VIPMud ``.set`` interpreter) compiles down to
registrations against this one engine, so a line is matched once regardless of
which dialect authored the rule. Side effects (send/speak/play/...) go through an
injectable :class:`EngineSink` so the engine is testable without a network, a
voice backend, or a UI.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from genericmud.automation.channels import ChannelRouter
from genericmud.model.buffer import Line
from genericmud.session.hub import SessionHub
from genericmud.sound.bus import SoundBus

if TYPE_CHECKING:
    from genericmud.session.diaglog import DiagnosticLog

# A pack-supplied regex trigger is matched against every incoming line, so a catastrophic-
# backtracking pattern (ReDoS) on a crafted line could hang the engine. The `regex` module is
# a `re` superset that honours a per-match timeout (and interrupts backtracking); fall back to
# stdlib `re` (no timeout) if it's somehow absent, so the engine still imports.
_MATCH_BUDGET_SECONDS = 0.25
try:
    import regex as _matcher

    _MATCH_KWARGS: dict[str, float] = {"timeout": _MATCH_BUDGET_SECONDS}
    _MatchTimeout: type[BaseException] = TimeoutError
except ImportError:  # pragma: no cover - regex is a declared dependency
    import re as _matcher  # type: ignore[no-redef]

    _MATCH_KWARGS = {}

    class _MatchTimeout(Exception):  # never raised by stdlib re; keeps the except clause valid
        ...


class EngineSink:
    """Side-effect surface the engine drives. Real wiring overrides these."""

    def send(self, text: str) -> None: ...
    def send_packet(self, data: bytes) -> None: ...
    def echo(self, text: str, channel: str = "main") -> None: ...
    def file_line(self, text: str, channel: str) -> None:
        """Keep ``text`` in the scrollback under ``channel``, without showing or speaking it."""
    def speak(self, text: str, channel: str = "main", interrupt: bool = False) -> None: ...
    def stop_speech(self) -> None: ...
    def play(
        self,
        file: str,
        channel: str = "sound",
        gain: float = 1.0,
        pan: float = 0.0,
        loop: bool = False,
    ) -> None: ...
    def stop(self, channel: str) -> None: ...
    def music(self, file: str, channel: str = "music") -> None: ...
    def schedule(self, delay: float, callback: Callable[[], None]) -> object | None: ...
    def recent_lines(self, count: int) -> list[str]:
        """The last ``count`` lines of the output, oldest first."""
        return []
    def ask(
        self, request: dict, while_waiting: Callable[[], None] | None = None
    ) -> object | None:
        """Show the player a pack's question and wait for the answer; None means cancelled.

        ``request["kind"]`` is "choice", "choices", "text" or "message"; ScriptApi's ask_*
        methods build each one and check the answer. ``while_waiting`` is called now and
        then until the answer comes. A front end with no dialogs answers None.
        """
        return None


def wildcard_regex(pattern: str) -> str:
    """The regex a wildcard pattern stands for: ``*`` captures any run, ``?`` one char.

    Each wildcard becomes a numbered capture group; everything else is literal. The
    MUSHclient and VIPMud front-ends translate their own pattern syntax themselves and
    register real regexes, so this is the native (Lua, Automation Manager) convention.
    """
    out: list[str] = []
    for ch in pattern:
        if ch == "*":
            out.append("(.*)")
        elif ch == "?":
            out.append("(.)")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def compile_pattern(pattern: str, regex: bool, *, whole: bool = False) -> re.Pattern[str]:
    """Compile a trigger/alias pattern.

    ``regex=True`` is used verbatim. Otherwise it's a wildcard pattern (see
    :func:`wildcard_regex`). ``whole=True`` makes a wildcard pattern match the entire
    text rather than a prefix of it: an alias "sc" is the command sc, not every command
    that happens to start with sc ("scan"), and "combo *" still takes the rest as its
    capture.
    """
    if regex:
        return _matcher.compile(pattern)
    body = wildcard_regex(pattern)
    if whole:
        body += r"\Z"
    return _matcher.compile(body)


@dataclass
class MatchContext:
    """Passed to a rule's callback when it fires."""

    line: Line
    wildcards: list[str]  # [0] = full match, [1..] = capture groups
    named: dict[str, str]
    engine: AutomationEngine


Callback = Callable[[MatchContext], None]


@dataclass
class _Rule:
    pattern: re.Pattern[str]
    callback: Callback | None
    priority: int = 0
    name: str = ""
    enabled: bool = True
    gag: bool = False
    gag_but_display: bool = False
    keep_evaluating: bool = True
    source: str = ""
    channel: str | None = None


def _wildcards(match: re.Match[str]) -> list[str]:
    return [match.group(0), *(g if g is not None else "" for g in match.groups())]


def _named(match: re.Match[str]) -> dict[str, str]:
    return {name: value if value is not None else "" for name, value in match.groupdict().items()}


class AutomationEngine:
    def __init__(self, sink: EngineSink | None = None, *, sound: SoundBus | None = None) -> None:
        self.sink = sink or EngineSink()
        self._triggers: list[_Rule] = []
        self._aliases: list[_Rule] = []
        self._keys: dict[str, Callback] = {}  # effective binding (last writer wins)
        # (key, source, callback) log: conflict reports + replay-rebuild on remove_source.
        self._key_bindings: list[tuple[str, str, Callback]] = []
        self._vars: dict[str, str] = {}
        self._gvars: dict[str, str] = {}
        # Latest structured values supplied by the MUD through GMCP/MSDP/MSSP. These stay
        # separate from script variables: a server update must not overwrite a user's setting,
        # while ScriptApi can still resolve either namespace for command templates.
        self._mud_vars: dict[str, object] = {}
        self.channels = ChannelRouter()  # output routing/policy, scriptable via ScriptApi
        self.sound = sound or SoundBus()  # per-category audio mixing, scriptable via ScriptApi
        self.hub: SessionHub | None = None  # cross-session bus (set by the app)
        self.session_name = ""  # this session's name, for broadcast-exclude
        self.connected = True  # transport state, maintained by the app (packs read it)
        self.diag: DiagnosticLog | None = None  # sound-path trace (set by the app)
        # The player's own sounds folder for this world (set by the window before packs
        # load). Unlike @sppath, which any pack can set, it is trusted for sound paths.
        self.sounds_dir: str | None = None
        # Seconds scripts have spent waiting on the player's answers to dialogs. Every
        # dialect's time limit leaves them out, whichever pack asked.
        self.player_time = 0.0
        # The server line the triggers are working on, so a pack's script can act on it
        # (MUSHclient packs file it under a channel with history_add).
        self.current_line: Line | None = None
        # False while a pack wants incoming lines kept quiet (Miriani-Next's Do Not Disturb).
        self.line_speech = True
        # (handle, source) pairs let a live script reload cancel only the retiring script set.
        self._timer_handles: list[tuple[object, str]] = []

    # --- registration ---

    def add_trigger(
        self,
        pattern: str,
        callback: Callback | None = None,
        *,
        regex: bool = False,
        priority: int = 0,
        name: str = "",
        gag: bool = False,
        gag_but_display: bool = False,
        keep_evaluating: bool = True,
        source: str = "",
        channel: str | None = None,
    ) -> _Rule:
        rule = _Rule(
            compile_pattern(pattern, regex),
            callback,
            priority,
            name,
            True,
            gag,
            gag_but_display,
            keep_evaluating,
            source,
            channel,
        )
        self._triggers.append(rule)
        self._triggers.sort(key=lambda r: -r.priority)
        return rule

    def add_alias(
        self,
        pattern: str,
        callback: Callback,
        *,
        regex: bool = False,
        priority: int = 0,
        name: str = "",
        keep_evaluating: bool = False,
        source: str = "",
    ) -> _Rule:
        # process_input anchors every alias at the start; a wildcard alias is anchored at the
        # end too, so it has to be the whole command. Regex aliases keep whatever their
        # author wrote.
        rule = _Rule(compile_pattern(pattern, regex, whole=True), callback, priority, name, True,
                     source=source, keep_evaluating=keep_evaluating)
        self._aliases.append(rule)
        self._aliases.sort(key=lambda r: -r.priority)
        return rule

    def remove_trigger(self, name: str) -> None:
        self._triggers = [rule for rule in self._triggers if rule.name != name]

    def remove_alias(self, name: str) -> None:
        self._aliases = [rule for rule in self._aliases if rule.name != name]

    def add_key(self, key: str, callback: Callback, *, source: str = "") -> None:
        self._keys[key.lower()] = callback
        self._key_bindings.append((key.lower(), source, callback))

    def remove_key(self, key: str, *, source: str = "") -> None:
        """Drop one source's bindings of ``key``; an earlier owner's binding comes back."""
        key = key.lower()
        self._key_bindings = [
            binding for binding in self._key_bindings if binding[:2] != (key, source)
        ]
        survivors = [callback for bound, _source, callback in self._key_bindings if bound == key]
        if survivors:
            self._keys[key] = survivors[-1]
        else:
            self._keys.pop(key, None)

    def registrations_by_source(self) -> dict[str, dict[str, list[str]]]:
        """Every trigger/alias/key grouped by the pack that registered it.

        Tokens are the regex/pattern string (triggers/aliases) or the key combo.
        Drives cross-pack conflict detection; read-only.
        """
        reg: dict[str, dict[str, list[str]]] = {}

        def bucket(source: str) -> dict[str, list[str]]:
            return reg.setdefault(source, {"trigger": [], "alias": [], "key": []})

        for rule in self._triggers:
            bucket(rule.source)["trigger"].append(rule.pattern.pattern)
        for rule in self._aliases:
            bucket(rule.source)["alias"].append(rule.pattern.pattern)
        for key, source, _callback in self._key_bindings:
            bucket(source)["key"].append(key)
        return reg

    # --- variables ---

    def get_var(self, name: str, default: str | None = "") -> str | None:
        return self._vars.get(name, self._gvars.get(name, default))

    def set_var(self, name: str, value: object) -> None:
        self._vars[name] = str(value)

    def remove_source(self, source: str) -> None:
        """Drop every rule and channel policy registered by one reloading source.

        Keys are rebuilt from the surviving bindings in registration order, preserving
        the last-writer-wins the original registrations produced.
        """
        self._triggers = [rule for rule in self._triggers if rule.source != source]
        self._aliases = [rule for rule in self._aliases if rule.source != source]
        if any(bind_source == source for _key, bind_source, _cb in self._key_bindings):
            self._key_bindings = [b for b in self._key_bindings if b[1] != source]
            # Replay the surviving bindings in order: last writer wins again, and a
            # combo the removed source had shadowed falls back to the earlier owner.
            self._keys = {key: callback for key, _source, callback in self._key_bindings}
        self.channels.remove_source(source)
        self.cancel_source_timers(source)

    def delete_var(self, name: str) -> None:
        self._vars.pop(name, None)

    def all_vars(self) -> dict[str, str]:
        """A snapshot of session variables (for pack-state persistence)."""
        return dict(self._vars)

    def get_gvar(self, name: str) -> str:
        return self._gvars.get(name, "")

    def delete_gvar(self, name: str) -> None:
        self._gvars.pop(name, None)

    def set_gvar(self, name: str, value: object) -> None:
        self._gvars[name] = str(value)

    # --- structured MUD variables ---

    @staticmethod
    def _mapping_item(mapping: dict, name: str) -> tuple[bool, object]:
        """Case-insensitive mapping lookup while preserving the server's original keys."""
        if name in mapping:
            return True, mapping[name]
        folded = name.casefold()
        for key, value in mapping.items():
            if str(key).casefold() == folded:
                return True, value
        return False, None

    def set_mud_var(self, name: str, value: object) -> None:
        """Record one latest GMCP/MSDP/MSSP value under its protocol-provided name."""
        if not name:
            return
        found, _value = self._mapping_item(self._mud_vars, name)
        if found:
            # Replace the existing spelling instead of accumulating HEALTH/health duplicates.
            folded = name.casefold()
            old_name = next(key for key in self._mud_vars if key.casefold() == folded)
            self._mud_vars[old_name] = value
        else:
            self._mud_vars[name] = value

    def resolve_mud_var(self, name: str) -> tuple[bool, object]:
        """Resolve ``HEALTH`` or a dotted path such as ``Char.Vitals.hp``.

        GMCP package names already contain dots, so resolution finds the longest matching
        top-level key first, then walks dictionaries (and zero-based list indexes) below it.
        """
        parts = str(name).split(".") if name else []
        for boundary in range(len(parts), 0, -1):
            found, value = self._mapping_item(self._mud_vars, ".".join(parts[:boundary]))
            if not found:
                continue
            for part in parts[boundary:]:
                if isinstance(value, dict):
                    found, value = self._mapping_item(value, part)
                    if not found:
                        return False, None
                elif isinstance(value, (list, tuple)) and part.isdecimal():
                    index = int(part)
                    if index >= len(value):
                        return False, None
                    value = value[index]
                else:
                    return False, None
            return True, value
        return False, None

    def get_mud_var(self, name: str, default: object = None) -> object:
        found, value = self.resolve_mud_var(name)
        return value if found else default

    def all_mud_vars(self) -> dict[str, object]:
        """A shallow snapshot of the latest structured server values."""
        return dict(self._mud_vars)

    # --- timers ---

    def schedule_timer(
        self, delay: float, callback: Callable[[], None], *, source: str = ""
    ) -> object | None:
        """Schedule a pack timer through the sink and track its handle.

        Tracked so :meth:`cancel_timers` can drop everything still pending at session close;
        a one-shot cue timer that outlives its tab would otherwise fire and start audio with no
        session left to stop it.
        """
        handle = self.sink.schedule(delay, callback)
        if handle is not None:
            self._timer_handles.append((handle, source))
        return handle

    def track_pending(self, handle: object, *, source: str = "") -> None:
        """Hold other cancellable pending work (a sound still downloading) with the timers,
        so session teardown and a source's reload cancel it the same way."""
        self._timer_handles.append((handle, source))

    def discard_timer(self, handle: object) -> None:
        """Forget a handle once it has fired, so the tracking list can't grow unbounded."""
        self._timer_handles = [pair for pair in self._timer_handles if pair[0] is not handle]

    def cancel_source_timers(self, source: str) -> None:
        """Cancel pending timers registered by one retiring script/pack source."""
        keep: list[tuple[object, str]] = []
        for handle, handle_source in self._timer_handles:
            if handle_source != source:
                keep.append((handle, handle_source))
                continue
            cancel = getattr(handle, "cancel", None)
            if cancel is not None:
                cancel()
        self._timer_handles = keep

    def cancel_timers(self) -> None:
        """Cancel every still-pending pack timer (session teardown)."""
        for handle, _source in self._timer_handles:
            cancel = getattr(handle, "cancel", None)
            if cancel is not None:
                cancel()
        self._timer_handles.clear()

    # --- evaluation ---

    def process_line(self, line: Line) -> Line:
        """Run triggers against an incoming line; mutate gag flags in place."""
        previous, self.current_line = self.current_line, line
        try:
            return self._run_triggers(line)
        finally:
            self.current_line = previous

    def _run_triggers(self, line: Line) -> Line:
        for rule in list(self._triggers):
            if not rule.enabled:
                continue
            try:
                match = rule.pattern.search(line.plain_text, **_MATCH_KWARGS)
            except _MatchTimeout:
                rule.enabled = False  # a pattern that backtracks past the budget is disabled
                if self.diag is not None:
                    # A trigger silently disabling itself is a soundpack going quiet -- trace it.
                    self.diag.event("trigger.timeout_disabled", source=rule.source or "?",
                                    pattern=rule.pattern.pattern, line=line.plain_text)
                continue
            if match is None:
                continue
            if self.diag is not None:
                self.diag.event(
                    "trigger.fire", source=rule.source or "?", pattern=rule.pattern.pattern,
                    line=line.plain_text,
                )
            if rule.channel is not None:
                line.channel = rule.channel
            if rule.gag or rule.gag_but_display:
                line.gagged = True
                line.display_when_gagged = rule.gag_but_display
            if rule.callback is not None:
                rule.callback(MatchContext(line, _wildcards(match), _named(match), self))
            if not rule.keep_evaluating:
                break
        return line

    def process_input(self, text: str) -> list[str]:
        """Run aliases against a user input line.

        Returns the lines to actually send: ``[text]`` when no alias matches, or
        ``[]`` when an alias consumes the input (its callback performs the sends).
        """
        for rule in list(self._aliases):
            if not rule.enabled:
                continue
            try:
                match = rule.pattern.match(text, **_MATCH_KWARGS)
            except _MatchTimeout:
                rule.enabled = False  # a pattern that backtracks past the budget is disabled
                if self.diag is not None:
                    self.diag.event("alias.timeout_disabled", source=rule.source or "?",
                                    pattern=rule.pattern.pattern, line=text)
                continue
            if match is None:
                continue
            if rule.callback is not None:
                rule.callback(MatchContext(Line(text), _wildcards(match), _named(match), self))
            if not rule.keep_evaluating:
                return []
        return [text]

    def press_key(self, key: str) -> bool:
        if not self.has_key(key):
            return False
        self._keys[key.lower()](MatchContext(Line(""), [], {}, self))
        return True

    def has_key(self, key: str) -> bool:
        """Whether a macro is bound to this combo.

        The UI asks this BEFORE swallowing a keypress: an unbound combo must fall
        through to the platform (menu access keys, accelerators) instead of dying
        in the macro path. Read cross-thread; dict membership is safe under the GIL.
        """
        callback = self._keys.get(key.lower())
        # A binding can be switched off for now (a VIPMud key whose class is disabled), and
        # then the keypress belongs to the platform again.
        return callback is not None and getattr(callback, "active", lambda: True)()

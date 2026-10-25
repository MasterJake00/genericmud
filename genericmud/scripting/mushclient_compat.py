"""MUSHclient compatibility: load `<muclient>` XML worlds/plugins + run their Lua.

Parses MUSHclient triggers/aliases and executes their `<script>` CDATA against a
sandboxed Lua runtime whose globals are MUSHclient's API (``Send``, ``Sound``,
``GetInfo``, ``DoAfterSpecial``, ``ColourNote``, ``GetVariable``...), backed by the
shared :class:`ScriptApi`. The same functions are also mirrored onto a ``world``
table, since real packs call both bare (``Sound(...)``) and through the world
object (``world.Sound(...)``). This lets Matt's existing plugins (e.g.
``/home/matt/erion/erion_gathering.xml``) and MUSHclient soundpacks run on the
genericMud engine unchanged.

Scope: covers the API surface real soundpacks use, audio included — ``Sound`` (the
BASS-backed call mudsoundpack.com packs use, with its ``volume=``/``pan=`` control
strings) and ``GetInfo`` directory codes (so ``GetInfo(67).."/sounds/x.ogg"``
resolves against the pack dir). Out of scope: the full plugin-suite surface
(LuaSocket, GUI windows, VBScript) and a few simplified semantics — notably
``DoAfterSpecial`` always runs its deferred text as Lua (the soundpack-standard
"send to script" case) rather than honouring every sendto code.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from fnmatch import fnmatchcase
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request
from uuid import uuid4

from genericmud.automation.engine import MatchContext
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.guard import ScriptGuard
from genericmud.scripting.lua_runtime import install_pack_require, make_sandboxed_runtime
from genericmud.scripting.sqlite_compat import make_sqlite_bridge

_WILDCARD_RE = re.compile(r"%(\d)")
_SEND_TO_SCRIPT = "12"
_DEFAULT_TRIGGER_SEQUENCE = "100"

_SOUND_CHANNEL = "sound"  # MUSHclient Sound() is a single-voice channel
_SOUND_BUFFERS = 10  # MUSHclient PlaySound offers buffers 1..10; 0 = first free
_VOLUME_MAX = 100.0  # MUSHclient volume is 0..100
_PAN_MAX = 100.0  # bass/MUSHclient pan is -100..100; the SoundBus wants -1..1
_HTTP_CHUNK_BYTES = 16 * 1024
_HTTP_MAX_BYTES = 32 * 1024 * 1024
_HTTP_TIMEOUT_SECONDS = 20


def _open_http(url: str):
    """Open a public HTTP(S) URL with the pack downloader's redirect/SSRF checks."""
    from genericmud.packs.vault import _secure_urlopen, _validate_url

    _validate_url(url)
    request = Request(url, headers={"User-Agent": "genericMud-soundpack-runtime"})
    return _secure_urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS)


def _buffer_channel(number: int) -> str:
    """The logical sound-bus channel for a MUSHclient PlaySound buffer."""
    return f"mush-{number}"
_AUDIO_CHANNEL_PREFIX = "erion-audio-"  # one bus channel per cue, so stop(id) can target it

# Lifecycle entry points MUSHclient calls on each plugin. All plugins share one _G here,
# so each plugin's hooks are captured (and the globals cleared) right after its script
# runs -- otherwise a later plugin would inherit or silently overwrite an earlier one's.
# Every known name is captured for isolation; dispatch() is only wired up for the
# sound-critical ones today (install/connect + the telnet pair that carries MSDP).
_LIFECYCLE_HOOKS = (
    "OnPluginInstall",
    "OnPluginConnect",
    "OnPluginDisconnect",
    "OnPluginClose",
    "OnPluginEnable",
    "OnPluginDisable",
    "OnPluginSaveState",
    "OnPluginTick",
    "OnPluginLineReceived",
    "OnPluginPartialLine",
    "OnPluginBroadcast",
    "OnPluginListChanged",
    "OnPluginCommandEntered",
    "OnPluginCommand",
    "OnPluginSend",
    "OnPluginSent",
    "OnPluginPacketReceived",
    "OnPluginPlaySound",
    "OnPluginTelnetOption",
    "OnPluginScreendraw",
    "OnPluginWorldOutputResized",
    "OnPluginGetFocus",
    "OnPluginLoseFocus",
    "OnPluginTabComplete",
    "OnPluginTelnetRequest",
    "OnPluginTelnetSubnegotiation",
)


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # lupa hands Lua numbers over as int/float; nil arrives as None
    except (TypeError, ValueError):
        return None
_MUSHCLIENT_VERSION = "5.06"

# MUSHclient's AddTrigger/AddTriggerEx flag bits (exposed to packs as `trigger_flag`).
_TRIGGER_FLAGS = {
    "Enabled": 1,
    "OmitFromLog": 2,
    "OmitFromOutput": 4,
    "KeepEvaluating": 8,
    "IgnoreCase": 16,
    "RegularExpression": 32,
    "ExpandVariables": 512,
    "Replace": 1024,
    "LowercaseWildcard": 2048,
    "Temporary": 16384,
    "OneShot": 32768,
}
_SCRIPT_ERROR_OK = 0  # MUSHclient eOK; nonzero = failure (packs rarely check)
_MOD_ORDER = ("ctrl", "alt", "shift")  # canonical combo order, matching the UI's _key_combo

# Client-shell plugins whose job genericMud already owns. Loading them runs Windows UI,
# editor, or self-update code that cannot contribute a sound cue here. They are represented as
# enabled virtual plugins so dependency managers remain satisfied, and the skip is reported.
_HOST_PLUGIN_REPLACEMENTS = {
    "current_output_window.xml": "genericMud owns the output window",
    "capture2dworld.xml": "genericMud owns output review windows",
    "capture2notepads.xml": "genericMud owns output review windows",
    "localedit.xml": "genericMud owns command editing",
    "log_manager.xml": "genericMud owns session logging",
    "mushclient_help.xml": "genericMud provides its own help",
    "mm_gmcp_mapper_gmcp.xml": "genericMud does not load MUSHclient miniwindow mappers",
    "output_functions.xml": "genericMud owns output review windows",
    "plugins_updater_v2.xml": "genericMud owns soundpack updates",
    "trace_to_notepad.xml": "genericMud provides diagnostics instead of Notepad tracing",
    "updater.xml": "genericMud owns soundpack updates",
    "update_watcher.xml": "genericMud owns soundpack updates",
    "version_check.xml": "genericMud owns update checks and client preferences",
    "lua_sapi.xml": "genericMud owns automatic speech output",
    "mushreader.xml": "genericMud owns automatic speech output",
    "sapi_speaker.xml": "genericMud owns automatic speech output",
    "text_to_speech.xml": "genericMud owns automatic speech output",
    "universal_text_to_speech.xml": "genericMud owns automatic speech output",
}
_NON_LUA_SCRIPT_SUFFIXES = frozenset({".js", ".pl", ".pys", ".vbs"})
_GMCP_HANDLER_ID = "3e7dedbe37e44942dd46d264"  # GMCP_handler_NJG broadcast identity


def _reduce_ints(args: tuple, op) -> int:
    values = [int(_to_float(a) or 0) for a in args]
    result = values[0] if values else 0
    for value in values[1:]:
        result = op(result, value)
    return result


def mushclient_pattern(text: str) -> str:
    """The regex MUSHclient matches a plain (non-regex) trigger or alias against.

    MUSHclient never matches the text as written: ``ConvertToRegularExpression`` (its
    Utilities.cpp, called with ``bWholeLine`` true for every trigger and alias it loads)
    wraps it in ``^...$`` so it has to be the whole line or the whole command, turns ``*``
    into a non-greedy ``(.*?)``, and treats everything else as a literal, ``?`` included.
    Packs are written for that: a plain trigger on "You are hungry." must not fire on
    someone saying the words, and an alias "vol" must not also catch "voldown".
    """
    parts = ["^"]
    for char in text:
        parts.append("(.*?)" if char == "*" else re.escape(char))
    parts.append("$")
    return "".join(parts)


def _normalize_key_spec(spec: object) -> str:
    """MUSHclient accelerator syntax -> a keymap combo, or "" when unusable.

    Real packs are inconsistent: "shift+f1", "ctrl +0", "alt + pageup",
    "shift+alt + left". Lowercase everything, strip spaces, and emit modifiers in
    the ctrl/alt/shift order the UI's _key_combo builds, key last.
    """
    parts = [part.strip().lower() for part in str(spec or "").split("+")]
    parts = [part for part in parts if part]
    keys = [part for part in parts if part not in _MOD_ORDER]
    if len(keys) != 1:
        return ""  # no key, or something like "ctrl+alt" alone
    mods = [mod for mod in _MOD_ORDER if mod in parts]
    return "+".join([*mods, keys[0]])


class MushclientPack:
    def __init__(self, api: ScriptApi, *, full_stdlib: bool = False) -> None:
        self._api = api
        self._base_dir = api.base_dir
        # Default @sppath to the pack dir when the session didn't pre-set it from world.sounds, so
        # _find_in_sounds_dir can locate bundled audio by basename when the pack's own
        # GetInfo()-anchored paths miss (e.g. Erion's split sounds/ + worlds/sounds/
        # layout). Mirrors the VIPMud default; the guard keeps a session-set
        # world.sounds path from being clobbered.
        if self._base_dir and not api.get_var("sppath"):
            api.set_var("sppath", self._base_dir)
        self._world_dir: str | None = None  # dir of the loaded world file; anchors GetInfo() paths
        self._world_path: Path | None = None
        self._client_dir = Path(self._base_dir).resolve() if self._base_dir else None
        self._world_id = uuid4().hex[:24]
        self._world_name = ""
        self._world_address = ""
        self._world_port = 0
        self._exposed: dict[str, dict] = {}  # ppi: plugin id -> {exposed name -> Lua fn}
        self._current_plugin = "world"  # whose script is loading now (for ppi.Expose)
        self._current_document: Path | None = None
        self._loaded_includes: set[Path] = set()  # resolved paths, so each file loads once
        self._include_errors: list[tuple[str, str]] = []  # plugins that failed to load (name, why)
        self._skipped_plugins: list[tuple[str, str]] = []  # optional/missing plugins (name, why)
        self._rule_errors: list[tuple[str, str]] = []  # malformed rules skipped in isolation
        self._external_script_errors: list[tuple[str, str]] = []
        self._module_errors: list[tuple[str, str]] = []
        self._hook_errors: list[tuple[str, str]] = []
        self._plugin_info: dict[str, dict[str, object]] = {}
        self._plugin_file_by_id: dict[str, Path] | None = None
        self._file_index: dict[str, list[Path]] | None = None
        self._hooks: dict[str, dict[str, object]] = {}  # plugin id -> {hook name -> Lua fn}
        self._arrays: dict[str, dict[str, str]] = {}  # MUSHclient Array* API backing store
        self._gmcp_values: dict[str, object] = {}
        self._options: dict[str, object] = {
            "enable_aliases": 1,
            "enable_scripts": 1,
            "enable_timers": 1,
            "enable_triggers": 1,
        }
        self._alpha_options: dict[str, str] = {}
        # AddTriggerEx-made rules, by name. One engine rule per name whose callback reads
        # this state, so a Replace re-registration mutates state instead of stacking a new
        # engine rule per keypress (Erion re-adds its announce triggers on every F-key).
        self._dynamic_triggers: dict[str, dict] = {}
        self._xml_rules: list[dict[str, object]] = []  # enabled state for XML rules/groups
        self._dyn_counter = 0  # unique engine-rule name per dynamic trigger, so retired ones
        self._unique_number = 0
        #                        (deleted / one-shot spent / pattern-replaced) get removed from
        #                        the engine instead of piling up as dead, still-scanned rules
        # MUSHclient targets Lua 5.1; trusted packs keep the full stdlib their
        # libraries assume (os/io/loadstring + the module(..., package.seeall) idiom).
        self._script_error_spoken = False  # speak the first fire-time script fault, trace the rest
        self._lua, install_hook = make_sandboxed_runtime(lua51=True, full_stdlib=full_stdlib)
        if full_stdlib:
            self._install_io_compat()
            self._install_os_compat()
        # Untrusted packs fail closed if the runaway-loop guard can't be installed; a trusted
        # pack is user-vouched arbitrary code, so a missing hook is acceptable there.
        self._guard = ScriptGuard(
            install_hook, require_hook=not full_stdlib, report=self._report_error
        )
        self._install_api()
        self._install_sendpkt()
        self._install_audio()
        self._install_nvda()
        self._install_utils()
        # Calls fn(option, <lua byte-string>) entirely on the Lua side: an MSDP payload
        # is rarely valid UTF-8, so it crosses the lupa boundary as a table of byte
        # values, never as a string (lupa's string conversion would raise or mangle it).
        self._payload_caller = self._lua.eval(
            "function(fn, option, t)\n"
            "  local parts = {}\n"
            "  for i = 1, #t do parts[i] = string.char(t[i]) end\n"
            "  return fn(option, table.concat(parts))\n"
            "end"
        )
        # Hand each plugin our own ppi (its bundled ppi.lua needs package.seeall, which the
        # sandbox strips). Then make any still-unimplemented host name a "black hole" that is
        # callable AND indexable (returns itself) -- so Window/InfoBox/etc. we don't implement
        # no-op (even Foo.bar.baz()) and the plugin loads + its sound path runs.
        # A black hole: callable AND self-indexing (returns itself), so Window/InfoBox/socket
        # and any other host API we don't implement no-op, even Foo.bar.baz(). It backs both an
        # unresolved require (native/external modules) and any unknown global, so a plugin loads
        # + its sound path runs regardless of the peripheral features it reaches for.
        black_hole = self._lua.eval(
            "setmetatable({}, {__call=function(t) return t end, __index=function(t) return t end})"
        )
        bass_bridge = self._make_bass_bridge()
        json_bridge = self._make_json_bridge()
        lfs_bridge = self._make_lfs_bridge()
        socket_bridge = self._make_socket_bridge()
        http_bridge, ltn12_bridge = self._make_http_bridges()
        socket_bridge.http = http_bridge
        sqlite_bridge = make_sqlite_bridge(
            self._lua, self._base_dir, lambda: self._client_dir
        )
        self._lua.globals().json = json_bridge
        self._lua.globals().ltn12 = ltn12_bridge
        self._lua.globals().socket = socket_bridge
        self._lua.globals().sqlite3 = sqlite_bridge
        install_pack_require(
            self._lua,
            self._base_dir,
            builtins={
                "ppi": self._make_ppi(),
                "miriani.lib.audio.bass": bass_bridge,
                "miriani/lib/audio/bass": bass_bridge,
                "json": json_bridge,
                "lfs": lfs_bridge,
                "ltn12": ltn12_bridge,
                "socket": socket_bridge,
                "socket.http": http_bridge,
                "sqlite3": sqlite_bridge,
            },
            fallback=black_hole,
            on_error=lambda name, path, exc: self._module_errors.append(
                (f"require({name}) [{path.name}]", f"{type(exc).__name__}: {exc}")
            ),
        )
        # Only API-shaped names fall into the black hole: MUSHclient functions are CapWords
        # (Sound, WindowCreate, BroadcastPlugin...) plus a few lowercase host libraries
        # (utils/rex/serialize). `bit` and `nvda` used to be here but are real shims now
        # (AddTriggerEx flag math; the pack speech object). A plain script variable (var,
        # dir, roomName) must read
        # back as nil -- assigning nil to a global DELETES it, so an unconditional fallback
        # made `if var ~= nil` true right after `var = nil` and Erion's OnPluginInstall
        # stored the black hole into every sound toggle instead of defaulting them to 1.
        self._lua.eval(
            "function(bh)\n"
            "  local hosted = {utils=true, rex=true, serialize=true}\n"
            "  setmetatable(_G, {__index = function(_, key)\n"
            "    if type(key) == 'string' and (string.match(key, '^%u') or hosted[key]) then\n"
            "      return bh\n"
            "    end\n"
            "    return nil\n"
            "  end})\n"
            "end"
        )(black_hole)

    def _install_io_compat(self) -> None:
        """Match MUSHclient's pack-relative file paths and legacy read modes."""
        original_open = self._lua.globals().io.open

        def open_file(
            filename: object = "", mode: object = "r", *_args: object
        ) -> object:
            name = str(filename or "")
            normalized = name.replace("\\", "/")
            is_windows_absolute = re.match(r"^[A-Za-z]:/", normalized) is not None
            if self._base_dir and not Path(normalized).is_absolute() and not is_windows_absolute:
                target = self._resolve_pack_directory(name)
                if target is None:
                    return None, "relative file path escapes the soundpack"
                name = target.as_posix()
            return original_open(name, str(mode or "r"))

        self._lua.globals().io.open = open_file
        self._lua.execute(
            "local methods = getmetatable(io.stdin).__index\n"
            "local original_read = methods.read\n"
            "methods.read = function(handle, ...)\n"
            "  local count, modes = select('#', ...), {...}\n"
            "  for i = 1, count do\n"
            "    if type(modes[i]) == 'string' then\n"
            "      local mode = string.match(modes[i], '^([alLn])%*$')\n"
            "      if mode then modes[i] = '*' .. mode end\n"
            "    end\n"
            "  end\n"
            "  return original_read(handle, unpack(modes, 1, count))\n"
            "end"
        )

    def _install_os_compat(self) -> None:
        """Handle Windows ``mkdir`` commands inside the installed pack on any host OS."""
        original_execute = self._lua.globals().os.execute

        def execute(command: object = "", *_args: object):
            text = str(command or "")
            match = re.fullmatch(
                r'\s*(?:md|mkdir)\s+"([^"]+)"(?:\s+2>\s*nul)?\s*', text, re.IGNORECASE
            )
            if match:
                path = self._resolve_pack_directory(match.group(1))
                if path is None:
                    return 1
                path.mkdir(parents=True, exist_ok=True)
                return 0
            return original_execute(text)

        self._lua.globals().os.execute = execute

    def _make_ppi(self):
        """A minimal in-process ppi (plugin-to-plugin interface): Expose registers a
        function under the loading plugin; Load returns a plugin's exposed functions."""
        ppi = self._lua.table()
        ppi.Expose = self._ppi_expose
        ppi.Load = self._ppi_load
        ppi.init = lambda *_a: None
        ppi.unload = lambda *_a: None
        return ppi

    def _make_bass_bridge(self):
        """MUSHclient BASS class surface backed by genericMud's sound bus."""
        api = self._api
        streams: list[dict[str, object]] = []

        def stream_create_file(*args: object):
            # Colon call: bass, mem, file, offset, length, flags.
            file = str(args[2] if len(args) > 2 else "")
            flags = int(_to_float(args[5]) or 0) if len(args) > 5 else 0
            state: dict[str, object] = {
                "active": False,
                "channel": f"mush-bass-{len(streams) + 1}",
                "file": file,
                "gain": 1.0,
                "pan": 0.0,
                "loop": bool(flags & 4),
            }
            streams.append(state)
            stream = self._lua.table()

            def play(*_args: object) -> int:
                api.play(
                    str(state["file"]),
                    channel=str(state["channel"]),
                    gain=float(state["gain"]),
                    pan=float(state["pan"]),
                    loop=bool(state["loop"]),
                )
                state["active"] = True
                return _SCRIPT_ERROR_OK

            def stop(*_args: object) -> int:
                api.stop(str(state["channel"]))
                state["active"] = False
                return _SCRIPT_ERROR_OK

            def set_attribute(_stream: object, attribute: object = 0, value: object = 0) -> int:
                number = int(_to_float(attribute) or 0)
                scalar = _to_float(value) or 0.0
                if number == 2:
                    state["gain"] = scalar
                    if state["active"]:
                        api.adjust(str(state["channel"]), gain=scalar)
                elif number == 3:
                    state["pan"] = scalar
                    if state["active"]:
                        api.adjust(str(state["channel"]), pan=scalar)
                return _SCRIPT_ERROR_OK

            stream.Play = play
            stream.Stop = stop
            stream.Free = stop
            stream.Pause = stop
            stream.IsActive = lambda *_a: 1 if state["active"] else 0
            stream.SetAttribute = set_attribute
            stream.SlideAttribute = set_attribute
            return stream

        def set_attribute(*args: object) -> int:
            # Colon call: bass, stream, attribute, value.
            if len(args) >= 4:
                setter = args[1]["SetAttribute"]
                setter(args[1], args[2], args[3])
            return _SCRIPT_ERROR_OK

        def free(*_args: object) -> int:
            for state in streams:
                if state["active"]:
                    api.stop(str(state["channel"]))
                    state["active"] = False
            return _SCRIPT_ERROR_OK

        bass = self._lua.eval("setmetatable({}, {__call = function(t) return t end})")
        bass.Init = lambda *_a: _SCRIPT_ERROR_OK
        bass.Free = free
        bass.GetConfig = lambda *_a: 0
        bass.GetVersion = lambda *_a: 0
        bass.SetConfig = lambda *_a: _SCRIPT_ERROR_OK
        bass.StreamCreateFile = stream_create_file
        bass.SetAttribute = set_attribute
        return bass

    def _make_json_bridge(self):
        """JSON codec for packs whose bundled implementation depends on native LPeg."""

        def from_lua(value: object):
            if not hasattr(value, "items"):
                return value
            items = list(value.items())
            integer_keys = [key for key, _item in items if isinstance(key, int)]
            if len(integer_keys) == len(items) and sorted(integer_keys) == list(
                range(1, len(items) + 1)
            ):
                by_key = dict(items)
                return [from_lua(by_key[index]) for index in range(1, len(items) + 1)]
            return {str(key): from_lua(item) for key, item in items}

        bridge = self._lua.table()
        bridge.decode = lambda value="", *_a: self._to_lua_value(json.loads(str(value)))
        bridge.encode = lambda value=None, *_a: json.dumps(
            from_lua(value), ensure_ascii=False, separators=(",", ":")
        )
        return bridge

    def _make_socket_bridge(self):
        """Non-network LuaSocket subset used by packs for a sub-second clock."""
        bridge = self._lua.table()
        bridge.gettime = lambda *_a: time.time()
        return bridge

    def _make_http_bridges(self):
        """Bounded LuaSocket HTTP GET plus the ``ltn12.sink.table`` contract."""
        deliver_bytes = self._lua.eval(
            "function(sink, bytes) local out = {} "
            "for i = 1, #bytes do out[i] = string.char(bytes[i]) end "
            "return sink(table.concat(out)) end"
        )
        make_table_sink = self._lua.eval(
            "function(target) return function(chunk) "
            "if chunk ~= nil then target[#target + 1] = chunk end return 1 end end"
        )

        def headers_table(headers: object):
            try:
                values = {str(key): str(value) for key, value in headers.items()}
            except AttributeError:
                values = {}
            return self._lua.table_from(values)

        def raw_request(options: object = "", *_args: object):
            if hasattr(options, "items"):
                url = str(options["url"] or "")
                sink = options["sink"]
            else:
                url = str(options or "")
                sink = None
            if not url:
                return None, 0, self._lua.table(), "missing URL"
            if sink is None:
                return None, 0, self._lua.table(), "missing response sink"
            try:
                with _open_http(url) as response:
                    status = int(getattr(response, "status", response.getcode()))
                    headers = headers_table(response.headers)
                    reason = str(getattr(response, "reason", ""))
                    length = response.headers.get("Content-Length")
                    if length is not None and int(length) > _HTTP_MAX_BYTES:
                        return None, 413, headers, "response exceeds sound download limit"
                    total = 0
                    while True:
                        chunk = response.read(_HTTP_CHUNK_BYTES)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > _HTTP_MAX_BYTES:
                            return None, 413, headers, "response exceeds sound download limit"
                        deliver_bytes(sink, self._lua.table_from(list(chunk)))
                    sink(None)
                    return 1, status, headers, reason
            except HTTPError as error:
                return None, error.code, headers_table(error.headers), str(error.reason)
            except Exception as error:  # noqa: BLE001 - LuaSocket reports transport errors
                return None, 0, self._lua.table(), f"{type(error).__name__}: {error}"

        http = self._lua.table()
        http.request = self._lua.eval(
            "function(raw_request, table_sink) return function(options) "
            "if type(options) == 'table' and options.sink ~= nil then "
            "return raw_request(options) end "
            "local body, request = {}, {} "
            "if type(options) == 'table' then "
            "for key, value in pairs(options) do request[key] = value end "
            "else request.url = options end "
            "request.sink = table_sink(body) "
            "local ok, status, headers, reason = raw_request(request) "
            "if not ok then return nil, status, headers, reason end "
            "return table.concat(body), status, headers, reason end end"
        )(raw_request, make_table_sink)
        sink = self._lua.table()
        sink.table = make_table_sink
        ltn12 = self._lua.table()
        ltn12.sink = sink
        return http, ltn12

    def _make_lfs_bridge(self):
        """LuaFileSystem reads confined to the installed pack."""
        base = Path(self._base_dir).resolve() if self._base_dir else None
        make_iterator = self._lua.eval(
            "function(items) local i = 0; return function() "
            "i = i + 1; return items[i] end end"
        )

        def resolve(value: object) -> Path | None:
            if base is None:
                return None
            normalized = str(value or "").replace("\\", "/")
            requested = Path(normalized)
            anchor = self._client_dir or base
            candidate = requested if requested.is_absolute() else anchor / requested
            try:
                candidate = candidate.resolve()
            except OSError:
                return None
            return candidate if candidate.is_relative_to(base) else None

        def attributes(value: object = "", key: object = None, *_args: object):
            path = resolve(value)
            if path is None or not path.exists():
                return None
            try:
                stat = path.stat()
            except OSError:
                return None
            values = {
                "access": stat.st_atime,
                "change": stat.st_ctime,
                "mode": "directory" if path.is_dir() else "file",
                "modification": stat.st_mtime,
                "size": stat.st_size,
            }
            if key is not None:
                return values.get(str(key))
            return self._lua.table_from(values)

        def directory(value: object = "", *_args: object):
            path = resolve(value)
            if path is None or not path.is_dir():
                return make_iterator(self._lua.table())
            try:
                names = [".", "..", *sorted(item.name for item in path.iterdir())]
            except OSError:
                names = []
            return make_iterator(self._lua.table_from(names))

        lfs = self._lua.table()
        lfs.attributes = attributes
        lfs.symlinkattributes = attributes
        lfs.currentdir = lambda *_a: (self._client_dir or base).as_posix() if base else ""
        lfs.dir = directory
        lfs.mkdir = lambda *_a: None
        lfs.rmdir = lambda *_a: None
        lfs.chdir = lambda *_a: None
        return lfs

    def _ppi_expose(self, name: object = "", fn: object = None) -> None:
        key = str(name)
        functions = self._exposed.setdefault(self._current_plugin, {})
        functions[key] = fn if fn is not None else self._lua.globals()[key]

    def _ppi_load(self, plugin_id: object = None):
        key = str(plugin_id or "")
        if key not in self._plugin_info:
            target = self._find_plugin_by_id(key)
            if target is not None and target not in self._loaded_includes:
                self._loaded_includes.add(target)
                reason = self._plugin_skip_reason(target)
                if reason:
                    self._mark_virtual_plugin(target)
                    self._skip_plugin(target.name, reason)
                else:
                    try:
                        self._load_path(target)
                    except Exception as exc:  # noqa: BLE001 - dependency remains isolated
                        self._include_errors.append(
                            (target.name, f"{type(exc).__name__}: {exc}")
                        )
        functions = self._exposed.get(key)
        return self._lua.table_from(functions) if functions else None

    def _gmcp_value(self, path: object = ""):
        value = self._gmcp_values.get(str(path or "").casefold())
        return self._to_lua_value(value)

    def _to_lua_value(self, value: object):
        if isinstance(value, dict):
            table = self._lua.table()
            for key, item in value.items():
                table[str(key)] = self._to_lua_value(item)
            return table
        if isinstance(value, list):
            table = self._lua.table()
            for index, item in enumerate(value, 1):
                table[index] = self._to_lua_value(item)
            return table
        return value

    def _broadcast_plugin(self, message: object = 0, text: object = "", *_args: object) -> int:
        info = self._plugin_info.get(self._current_plugin, {})
        self.dispatch(
            "OnPluginBroadcast", int(_to_float(message) or 0), self._get_plugin_id(),
            str(info.get("name", "")), str(text or ""),
        )
        return _SCRIPT_ERROR_OK

    def _get_plugin_id(self, *_args: object) -> str:
        return "" if self._current_plugin == "world" else self._current_plugin

    def _get_plugin_name(self, *_args: object) -> str:
        info = self._plugin_info.get(self._current_plugin, {})
        return str(info.get("name", ""))

    def _is_plugin_installed(self, plugin_id: object = "", *_args: object) -> bool:
        return str(plugin_id or "") in self._plugin_info

    def _get_plugin_info(self, plugin_id: object = "", info_type: object = 0, *_args: object):
        info = self._plugin_info.get(str(plugin_id or ""))
        if info is None:
            return None
        code = int(_to_float(info_type) or 0)
        if code == 17:  # enabled
            return bool(info["enabled"])
        if code == 20:  # plugin directory, including trailing separator
            path = info.get("path")
            return path.parent.as_posix() + "/" if isinstance(path, Path) else ""
        return info.get("name", "") if code == 1 else ""

    def _enable_plugin(self, plugin_id: object = "", enabled: object = True, *_args: object) -> int:
        info = self._plugin_info.get(str(plugin_id or ""))
        if info is None:
            return 1
        info["enabled"] = bool(enabled)
        return _SCRIPT_ERROR_OK

    def _load_plugin_api(self, filename: object = "", *_args: object) -> int:
        name = str(filename or "")
        target = self._resolve_pack_file(name)
        if target is None:
            self._skip_plugin(name, "required plugin is not bundled with the pack")
            return 1
        if target in self._loaded_includes:
            return _SCRIPT_ERROR_OK
        self._loaded_includes.add(target)
        reason = self._plugin_skip_reason(target)
        if reason:
            self._mark_virtual_plugin(target)
            self._skip_plugin(target.name, reason)
            return _SCRIPT_ERROR_OK
        try:
            self._load_path(target)
        except Exception as exc:  # noqa: BLE001 - dependency failure stays local to that plugin
            self._include_errors.append((name, f"{type(exc).__name__}: {exc}"))
            return 1
        return _SCRIPT_ERROR_OK

    def _dofile(self, filename: object = "", *_args: object):
        """Pack-confined `dofile`; a missing optional support file is a recorded no-op."""
        name = str(filename or "")
        target = self._resolve_pack_file(name)
        if target is None:
            self._skip_plugin(name, "Lua support file is not bundled with the pack")
            return None
        try:
            return self._load_path(target)
        except Exception as exc:  # noqa: BLE001 - keep the containing sound plugin usable
            self._external_script_errors.append((name, f"{type(exc).__name__}: {exc}"))
            return None

    # --- MUSHclient global API ---

    def _install_api(self) -> None:
        api = self._api

        def set_variable(name: object = "", value: object = "", *_args: object) -> int:
            api.set_var(str(name), value)
            return _SCRIPT_ERROR_OK

        def delete_variable(name: object = "", *_args: object) -> int:
            api.delete_var(str(name))
            return _SCRIPT_ERROR_OK

        funcs = {
            "Send": api.send,
            "SendNoEcho": api.send,
            # Execute = "as if typed": aliases match first (Erion's historyadd Executes
            # history_add, which its own channel_history alias consumes); send-through only
            # for what no alias claims.
            "Execute": api.execute,
            "Note": api.echo,
            "ColourNote": self._colour_note,
            # nil (not "") for an unset variable -- MUSHclient semantics. Erion's
            # OnPluginInstall does `if GetVariable(...) ~= nil` to keep saved toggle
            # settings; an ""-for-unset answer makes it adopt "" and every toggle-gated
            # sound stays off.
            "GetVariable": lambda name="": api.get_var(str(name), None),
            "SetVariable": set_variable,
            "GetOption": self._get_option,
            "SetOption": self._set_option,
            "GetAlphaOption": self._get_alpha_option,
            "SetAlphaOption": self._set_alpha_option,
            "GetAlphaOptionList": self._get_alpha_option_list,
            "GetGlobalOption": self._get_global_option,
            "GetGlobalOptionList": self._get_global_option_list,
            # The Array* trio MSDP packs use for state (room name etc.). Real bindings,
            # not black-holed: a black-holed ArrayGet returns a table, and concatenating
            # that raises inside the plugin's subnegotiation handler.
            "ArrayCreate": self._array_create,
            "ArraySet": self._array_set,
            "ArrayGet": self._array_get,
            # Truly delete: MUSHclient's GetVariable answers nil after a delete, and
            # packs distinguish nil ("use my default") from "" (a saved empty value).
            "DeleteVariable": delete_variable,
            # Real transport state: Erion's OnPluginTick branches on it to run its
            # music engine vs. stop everything (the black hole answered truthy-table).
            "IsConnected": lambda *_a: api.is_connected(),
            "Version": lambda *_a: _MUSHCLIENT_VERSION,
            "GetUniqueID": lambda *_a: uuid4().hex[:24],
            "GetUniqueNumber": self._get_unique_number,
            "GetWorldID": lambda *_a: self._world_id,
            "GetWorldList": lambda *_a: self._lua.table_from(
                [self._world_name] if self._world_name else []
            ),
            "WorldName": lambda *_a: self._world_name,
            "WorldAddress": lambda *_a: self._world_address,
            "WorldPort": lambda *_a: self._world_port,
            "GetPluginList": lambda *_a: self._lua.table_from(list(self._plugin_info)),
            "Trim": lambda value="", *_a: str(value).strip(),
            "GetPluginID": self._get_plugin_id,
            "GetPluginName": self._get_plugin_name,
            "GetPluginInfo": self._get_plugin_info,
            "IsPluginInstalled": self._is_plugin_installed,
            "LoadPlugin": self._load_plugin_api,
            "EnablePlugin": self._enable_plugin,
            "dofile": self._dofile,
            "gmcp": self._gmcp_value,
            "BroadcastPlugin": self._broadcast_plugin,
            "Accelerator": self._accelerator,
            "AcceleratorTo": self._accelerator_to,
            "AddTriggerEx": self._add_trigger_ex,
            "DeleteTrigger": self._delete_trigger,
            "CallPlugin": self._call_plugin,
            "EnableTrigger": self._enable_trigger,
            "EnableTriggerGroup": self._enable_trigger_group,
            "EnableAlias": self._enable_alias,
            "EnableAliasGroup": self._enable_alias_group,
            "EnableGroup": self._enable_group,
            "EnableTimer": lambda *_a: _SCRIPT_ERROR_OK,
            "AddTimer": self._add_timer_api,
            "Hyperlink": lambda *_a: None,
            "GetSoundKeyword": lambda *_a: "",
            "PlaySound": self._play_sound,
            "StopSound": self._stop_sound,
            "Sound": self._sound,
            "GetInfo": self._get_info,
            "DoAfterSpecial": self._do_after_special,
        }
        g = self._lua.globals()
        for name, fn in funcs.items():
            g[name] = fn
        # Packs call both bare (Sound(...)) and through the world object
        # (world.Sound(...), world.getvariable(...)). Mirror funcs onto a world
        # table, with lowercase aliases for the world.lowercase() callers.
        world = self._lua.table()
        for name, fn in funcs.items():
            world[name] = fn
            world[name.lower()] = fn
        g.world = world
        # Real `bit` ops and the AddTrigger flag/colour constants. These were black-holed,
        # which broke every runtime registration: bit.bor returned a table and
        # trigger_flag.Enabled was nil, so AddTriggerEx got garbage flags (Erion's F-key
        # report triggers are all registered this way).
        bit = self._lua.table()
        bit.bor = lambda *a: _reduce_ints(a, lambda x, y: x | y)
        bit.band = lambda *a: _reduce_ints(a, lambda x, y: x & y)
        bit.bxor = lambda *a: _reduce_ints(a, lambda x, y: x ^ y)
        bit.bnot = lambda x=0: ~(int(_to_float(x) or 0)) & 0xFFFFFFFF
        bit.lshift = (
            lambda x=0, n=0: (int(_to_float(x) or 0) << int(_to_float(n) or 0)) & 0xFFFFFFFF
        )
        bit.rshift = (
            lambda x=0, n=0: (int(_to_float(x) or 0) & 0xFFFFFFFF)
            >> int(_to_float(n) or 0)
        )
        g.bit = bit
        trigger_flag = self._lua.table()
        for flag_name, value in _TRIGGER_FLAGS.items():
            trigger_flag[flag_name] = value
        g.trigger_flag = trigger_flag
        sendto = self._lua.table()
        sendto.world = 0
        sendto.execute = 10
        sendto.script = int(_SEND_TO_SCRIPT)
        g.sendto = sendto
        timer_flag = self._lua.table()
        timer_flag.Enabled = 1
        timer_flag.AtTime = 2
        timer_flag.OneShot = 4
        timer_flag.ActiveWhenClosed = 32
        timer_flag.Replace = 1024
        timer_flag.Temporary = 16384
        g.timer_flag = timer_flag
        error_code = self._lua.table()
        error_code.eOK = _SCRIPT_ERROR_OK
        error_code.eInvalidObjectLabel = 1
        error_code.ePluginFileNotFound = 2
        error_code.eProblemsLoadingPlugin = 3
        g.error_code = error_code
        error_desc = self._lua.table()
        error_desc[_SCRIPT_ERROR_OK] = "No error"
        error_desc[1] = "Invalid object label"
        error_desc[2] = "Plugin file not found"
        error_desc[3] = "Problems loading plugin"
        g.error_desc = error_desc
        custom_colour = self._lua.table()
        custom_colour.NoChange = -1
        for i in range(1, 17):
            custom_colour[f"Custom{i}"] = i - 1
        g.custom_colour = custom_colour

    def _install_audio(self) -> None:
        """Provide the ``audio`` global that bass.dll-backed packs (Erion) play every cue through.

        Erion's sound engine (LuaAudio.xml) routes all game audio through ``audio.play`` /
        ``audio.playDelay`` -- and MSDP dispatch reaches it via ppi -- NOT through ``Sound()``.
        Without ``audio`` those calls hit the compat black-hole and no cue is ever heard, even
        though the pack loads its triggers. Map the sound-producing methods onto the ScriptApi
        (one bus channel per cue id, so ``stop(id)`` works); the DSP-only rest (pan/pitch/fades)
        no-op via the table's ``__index``. ``play``'s loop flag is honoured only for an explicit
        ``1`` (LuaAudio's music case) -- a stuck looping combat cue is worse than one that doesn't.
        """
        api = self._api
        channels: dict[int, str] = {}
        next_id = [1]

        def _alloc() -> tuple[int, str]:
            if len(channels) > 512:
                # Opportunistically forget finished cues so a marathon session doesn't grow
                # the id map forever. Only finished ones: a live loop must stay stoppable by id.
                for cid, ch in list(channels.items()):
                    if not api.is_playing(ch):
                        del channels[cid]
            cue_id = next_id[0]
            next_id[0] += 1
            channel = f"{_AUDIO_CHANNEL_PREFIX}{cue_id}"
            channels[cue_id] = channel
            return cue_id, channel

        def _gain(vol: object) -> float:
            value = _to_float(vol)
            return value / _VOLUME_MAX if value is not None else 1.0

        def _pan(pan: object) -> float:
            value = _to_float(pan)
            return max(-1.0, min(1.0, value / _PAN_MAX)) if value is not None else 0.0

        def _start(file: object, loop: object, pan: object, vol: object, delay: float) -> int:
            if not str(file or ""):
                return 0
            cue_id, channel = _alloc()
            gain, pan_value, looped = _gain(vol), _pan(pan), _to_float(loop) == 1

            def fire() -> None:
                api.play(str(file), channel=channel, gain=gain, pan=pan_value, loop=looped)

            if delay > 0:
                api.add_timer(delay, fire)
            else:
                fire()
            return cue_id

        def play(file: object = "", loop: object = 0, pan: object = None, vol: object = None,
                 *_rest: object) -> int:
            return _start(file, loop, pan, vol, 0.0)

        def play_delay(file: object = "", delay: object = 0, pan: object = None, vol: object = None,
                       *_rest: object) -> int:
            return _start(file, 0, pan, vol, max(_to_float(delay) or 0.0, 0.0))

        def play_delay_looped(file: object = "", delay: object = 0, pan: object = None,
                              vol: object = None, *_rest: object) -> int:
            return _start(file, 1, pan, vol, max(_to_float(delay) or 0.0, 0.0))

        def stop(cue_id: object = 0, *_rest: object) -> None:
            if _to_float(cue_id) == 0:  # bass convention: id 0 stops every cue
                api.flush()
                return
            channel = channels.pop(int(_to_float(cue_id) or 0), None)
            if channel is not None:
                api.stop(channel)

        def is_playing(cue_id: object = 0, *_rest: object) -> int:
            # Truthful per-cue status. Erion's ambience/music switching is gated on
            # ppi.isPlaying(old): a hardcoded 0 told it the old cue was already done,
            # so it started the new one WITHOUT stopping the old -- ambiences stacking
            # on every room change, area music piling up copy on copy.
            channel = channels.get(int(_to_float(cue_id) or 0))
            return 1 if channel is not None and api.is_playing(channel) else 0

        def fadeout(cue_id: object = 0, *_rest: object) -> None:
            # No DSP fade in the bus; an immediate stop is the correct end state
            # (this is how the pack retires the outgoing ambience/music).
            stop(cue_id)

        def slide_vol(cue_id: object = 0, vol: object = None, *_rest: object) -> None:
            # bass slides the cue's volume over time; the end state is what matters:
            # 0 is a fade-to-stop, anything else lands the cue at that level (no ramp).
            target = _to_float(vol)
            if target == 0:
                stop(cue_id)
                return
            channel = channels.get(int(_to_float(cue_id) or 0))
            if channel is not None and target is not None:
                api.adjust(channel, gain=target / _VOLUME_MAX)

        # A table backed by a no-op __index, so any bass method we don't implement
        # (pan/freq/pitch/slidePan/slidePitch/dll) is safely callable and just does nothing.
        audio = self._lua.eval("setmetatable({}, {__index = function() return function() end end})")
        audio.play = play
        audio.playLooped = lambda file="", *_a: _start(file, 1, None, None, 0.0)
        audio.playDelay = play_delay
        audio.playDelayLooped = play_delay_looped
        audio.stop = stop
        audio.fadeout = fadeout
        audio.slideVol = slide_vol
        audio.free = lambda *_a: api.flush()
        audio.getVolume = lambda *_a: _VOLUME_MAX
        audio.isPlaying = is_playing
        const = self._lua.table()
        device = self._lua.table()
        device.stereo = 0x8000
        const.device = device
        audio.const = const
        audio.Init = lambda *_a: _SCRIPT_ERROR_OK
        self._lua.globals().audio = audio

    def _install_nvda(self) -> None:
        """Provide the ``nvda`` object MushReader.dll would have installed.

        The pack's whole speech surface funnels through it: ``tts_interrupt`` (how the
        F-key hp/mana reports and the history browser talk) does ``nvda.stop(); say(...)``
        with ``say`` wrapping ``nvda.say``. As a black hole those were silent no-ops --
        the report chain ran perfectly and said nothing. ``say`` speaks through the
        engine (own channel, so channel policy can tune it), ``stop`` cuts current
        speech (the interrupt half of the idiom). Every OTHER method must be callable
        but return nil -- the pack probes ``nvda.jaws_running()`` and a truthy
        black-hole answer sent speech to a JAWS object that doesn't exist.
        """
        api = self._api
        nvda = self._lua.eval(
            "setmetatable({}, {__index = function() return function() return nil end end})"
        )
        nvda.say = lambda text="", *_a: api.speak(str(text).strip(), channel="pack")
        nvda.stop = lambda *_a: api.stop_speech()
        self._lua.globals().nvda = nvda

    def _install_utils(self) -> None:
        """Provide MUSHclient's scalar/string and confined directory utilities."""

        def split(value: object = "", delimiter: object = "", maximum: object = 0, *_args):
            text = str(value)
            separator = str(delimiter)
            limit = int(_to_float(maximum) or 0)
            if not separator:
                parts = [text]
            else:
                parts = text.split(separator, limit if limit > 0 else -1)
            return self._lua.table_from(parts)

        def readdir(specification: object = "", *_args):
            if self._base_dir is None:
                return None
            base = Path(self._base_dir).resolve()
            anchor = self._client_dir or base
            normalized = str(specification or "").replace("\\", "/")
            requested = Path(normalized)
            target = requested if requested.is_absolute() else anchor / requested
            if target.is_dir():
                directory, wildcard = target, "*"
            else:
                directory, wildcard = target.parent, target.name or "*"
            try:
                directory = directory.resolve()
            except OSError:
                return None
            if not directory.is_dir() or not directory.is_relative_to(base):
                return None
            if wildcard == "*.*":  # Windows treats *.* as every entry.
                wildcard = "*"
            result = self._lua.table()
            matched = False
            try:
                children = sorted(directory.iterdir(), key=lambda path: path.name.casefold())
            except OSError:
                return None
            for child in children:
                if not fnmatchcase(child.name.casefold(), wildcard.casefold()):
                    continue
                try:
                    resolved = child.resolve()
                    stat = resolved.stat()
                except OSError:
                    continue
                if not resolved.is_relative_to(base):
                    continue
                metadata = self._lua.table()
                metadata.size = stat.st_size
                metadata.directory = resolved.is_dir()
                metadata.hidden = child.name.startswith(".")
                metadata.readonly = not bool(stat.st_mode & 0o200)
                metadata.normal = not metadata.directory
                metadata.write_time = stat.st_mtime
                result[child.name] = metadata
                matched = True
            return result if matched else None

        utils = self._lua.eval(
            "setmetatable({}, {__index = function() "
            "return function() return nil end end})"
        )
        utils.split = split
        utils.readdir = readdir
        self._lua.globals().utils = utils

    def _colour_note(self, *args: object) -> None:
        # ColourNote(fg, bg, text [, fg, bg, text]...) — concatenate the text parts.
        texts = [str(args[i]) for i in range(2, len(args), 3)]
        self._api.echo("".join(texts))

    def _array_create(self, name: object = "") -> None:
        self._arrays.setdefault(str(name), {})

    def _array_set(self, name: object = "", key: object = "", value: object = "") -> None:
        self._arrays.setdefault(str(name), {})[str(key)] = str(value)

    def _array_get(self, name: object = "", key: object = "") -> str | None:
        return self._arrays.get(str(name), {}).get(str(key))  # nil when absent (MUSHclient)

    def _get_option(self, name: object = "", *_args: object) -> object:
        return self._options.get(str(name), 0)

    def _get_unique_number(self, *_args: object) -> int:
        self._unique_number += 1
        return self._unique_number

    def _add_timer_api(
        self,
        _name: object = "",
        hours: object = 0,
        minutes: object = 0,
        seconds: object = 0,
        response: object = "",
        _flags: object = 0,
        script_name: object = "",
        *_args: object,
    ) -> int:
        delay = (
            (_to_float(hours) or 0.0) * 3600
            + (_to_float(minutes) or 0.0) * 60
            + (_to_float(seconds) or 0.0)
        )
        script = str(script_name or "")
        text = str(response or "")

        def fire() -> None:
            target: object = self._lua.globals()
            for part in script.split(".") if script else ():
                target = target[part]
                if target is None:
                    break
            if target is not None and script:
                self._guard.run(target, str(_name or ""))
            elif text:
                self._api.send(text)

        self._api.add_timer(max(delay, 0.0), fire)
        return _SCRIPT_ERROR_OK

    def _set_option(self, name: object = "", value: object = 0, *_args: object) -> int:
        self._options[str(name)] = value
        return _SCRIPT_ERROR_OK

    def _get_alpha_option(self, name: object = "", *_args: object) -> str:
        return self._alpha_options.get(str(name), "")

    def _set_alpha_option(self, name: object = "", value: object = "", *_args: object) -> int:
        self._alpha_options[str(name)] = str(value)
        return _SCRIPT_ERROR_OK

    def _get_alpha_option_list(self, *_args: object):
        return self._lua.table_from(sorted(self._alpha_options))

    def _get_global_option(self, name: object = "", *_args: object) -> object:
        defaults = {
            "ConfirmBeforeClosingMushclient": 1,
            "ConfirmBeforeSavingVariables": 0,
            "F1macro": 1,
            "OpenActivityWindow": 0,
            "SmoothScrolling": 0,
            "SmootherScrolling": 0,
        }
        return defaults.get(str(name), 0)

    def _get_global_option_list(self, *_args: object):
        return self._lua.table()

    def _install_sendpkt(self) -> None:
        """Bind ``SendPkt`` via a Lua-side byte-table trampoline.

        The packet is a pre-framed telnet sequence (IAC SB ... IAC SE) full of bytes
        that are invalid UTF-8, so the Lua string must never cross the lupa boundary
        directly -- the runtime's string conversion would raise. The trampoline
        explodes it into a table of byte values; Python reassembles and sends verbatim
        (no re-framing: SendPkt's contract is that the caller built the framing).
        """
        make_sendpkt = self._lua.eval(
            "function(deliver)\n"
            "  return function(data)\n"
            "    data = tostring(data or '')\n"
            "    local bytes = {}\n"
            "    for i = 1, #data do bytes[i] = string.byte(data, i) end\n"
            "    deliver(bytes)\n"
            "  end\n"
            "end"
        )
        sendpkt = make_sendpkt(self._deliver_packet)
        globals_ = self._lua.globals()
        globals_["SendPkt"] = sendpkt
        globals_.world["SendPkt"] = sendpkt  # packs also call through the world object

    def _report_error(self, error: Exception) -> None:
        """A fire-time hook/timer fault is contained by the guard; trace every one and speak the
        first, so a broken pack callback (a missing accessibility cue) isn't silently swallowed."""
        if self._api.diag is not None:
            self._api.diag.event(
                "script.error", source=self._api.source or "?",
                error=f"{type(error).__name__}: {error}",
            )
        if not self._script_error_spoken:
            self._script_error_spoken = True
            self._api.speak(
                f"A soundpack script error occurred: {type(error).__name__}", channel="system"
            )

    def _deliver_packet(self, table: object) -> None:
        data = bytes(bytearray(table[i] for i in range(1, len(table) + 1)))
        self._api.send_packet(data)

    def _play_sound(
        self,
        buffer: object = 0,
        file: str = "",
        loop: object = False,
        volume: object = 100,
        pan: object = 0,
    ) -> None:
        # MUSHclient PlaySound(buffer, file, loop, volume, pan): volume 0..100, pan -100..100.
        # Both were dropped before, so every cue played full-volume and centered -- pan carries
        # directional information for a blind user, so that's a real accessibility loss.
        #
        # The buffer selects one of 10 concurrent slots; ignoring it collapsed every cue
        # onto one channel, so a one-shot on buffer 2 cut off a loop started on buffer 1.
        # Buffer 0 means "the first free buffer, or buffer 1 if none are free" (gammon.com.au
        # PlaySound docs). An empty filename modifies the sound already playing in that
        # buffer (volume/pan) rather than starting one; stopping is StopSound's job.
        vol = _to_float(volume)
        pan_value = _to_float(pan)
        gain = vol / _VOLUME_MAX if vol is not None else 1.0
        pan_out = max(-1.0, min(1.0, pan_value / _PAN_MAX)) if pan_value is not None else 0.0
        number = int(_to_float(buffer) or 0)
        file_text = str(file or "")
        if not file_text:
            if 1 <= number <= _SOUND_BUFFERS:
                self._api.adjust(_buffer_channel(number), gain=gain, pan=pan_out)
            return  # buffer 0 selects a FREE slot: nothing is playing there to modify
        if number == 0:
            number = next(
                (
                    n
                    for n in range(1, _SOUND_BUFFERS + 1)
                    if not self._api.is_playing(_buffer_channel(n))
                ),
                1,
            )
        number = min(max(number, 1), _SOUND_BUFFERS)
        self._api.play(
            file_text, channel=_buffer_channel(number), gain=gain, pan=pan_out, loop=bool(loop)
        )

    def _stop_sound(self, buffer: object = 0) -> None:
        """MUSHclient ``StopSound(buffer)``: stop one of the 10 sound buffers, 0 = all."""
        number = int(_to_float(buffer) or 0)
        if number == 0:
            for n in range(1, _SOUND_BUFFERS + 1):
                self._api.stop(_buffer_channel(n))
        elif 1 <= number <= _SOUND_BUFFERS:
            self._api.stop(_buffer_channel(number))

    def _sound(self, arg: object = "", *_rest: object) -> None:
        """MUSHclient ``Sound``: a path plays it; a ``key=value`` string is a control
        directive (``volume=``/``pan=``/``freq=``) for the current cue."""
        text = str(arg)
        if "=" in text:
            self._sound_control(text)
        elif text:
            self._api.play(text, channel=_SOUND_CHANNEL)

    def _sound_control(self, directive: str) -> None:
        key, _, raw = directive.partition("=")
        key = key.strip().lower()
        try:
            level = float(raw)
        except ValueError:
            return
        if key == "pan":
            # Live pan on the playing cue -- directional cues are how a pack tells a blind
            # player WHERE something happened, so dropping this loses real information.
            self._api.adjust(_SOUND_CHANNEL, pan=max(-1.0, min(1.0, level / _PAN_MAX)))
        elif key != "volume":
            return  # freq: no live pitch control in the bus — accept, ignore
        elif level <= 0:
            self._api.stop(_SOUND_CHANNEL)  # "volume=0" is the soundpack idiom for stop
        else:
            # adjust() re-levels the PLAYING cue; set_volume would permanently drop the whole
            # sound category (every future cue) and wouldn't even change the running loop.
            self._api.adjust(_SOUND_CHANNEL, gain=level / _VOLUME_MAX)

    def _get_info(self, code: object = 0) -> str:
        """Return the file/path/version selectors used by MUSHclient soundpacks."""
        try:
            number = int(code)
        except (TypeError, ValueError):
            return ""

        client = self._client_dir
        world_dir = Path(self._world_dir) if self._world_dir else client

        def directory(path: Path | None) -> str:
            return path.as_posix().rstrip("/") + "/" if path is not None else ""

        if number == 1:  # current world address
            return self._world_address
        if number == 2:  # current world name
            return self._world_name
        if number == 54:  # current world file pathname
            return self._world_path.as_posix() if self._world_path is not None else ""
        if number == 56:  # MUSHclient application path, with trailing separator
            return directory(client)
        if number == 57:  # default world directory
            return directory(client / "worlds" if client is not None else world_dir)
        if number == 58:  # default log directory
            return directory(client / "logs" if client is not None else None)
        if number == 59:  # default script directory
            return directory(client / "scripts" if client is not None else None)
        if number == 60:  # default plugin directory
            return directory(client / "worlds" / "plugins" if client is not None else None)
        if number in (64, 66, 68):  # current, application, and startup directories
            return directory(client)
        if number == 67:  # directory containing the current world file
            return directory(world_dir)
        if number == 72:
            return _MUSHCLIENT_VERSION
        if number == 74:  # default sounds directory
            return directory(client / "sounds" if client is not None else None)
        if number == 82:  # preferences DB pathname; host-owned and never opened by packs
            return (client / "MUSHclient_prefs.sqlite").as_posix() if client is not None else ""
        return ""

    def _accelerator(self, key: object = "", send: object = "", *_rest: object) -> None:
        """MUSHclient ``Accelerator(key, send)``: bind a hotkey that runs ``send`` as if
        typed. This is how a pack ships its keyboard UI -- Erion binds 54 of these
        (F1 hp report, F7 recall, the whole alt+arrows history browser) -- so a black
        hole here meant a pack loaded with zero working hotkeys (``pack.counts keys=0``).
        """
        combo = _normalize_key_spec(key)
        command = str(send or "")
        if not combo or not command:
            return
        self._api.add_key(combo, lambda _ctx, cmd=command: self._api.execute(cmd))

    def _accelerator_to(
        self,
        key: object = "",
        send: object = "",
        destination: object = 0,
        *_rest: object,
    ) -> int:
        """Bind a key to Lua when ``sendto.script`` is requested, else typed input."""
        combo = _normalize_key_spec(key)
        command = str(send or "")
        if not combo or not command:
            return 1
        if int(_to_float(destination) or 0) == int(_SEND_TO_SCRIPT):
            callback = self._compile(command)
            self._api.add_key(combo, lambda _ctx, fn=callback: self._guard.run(fn))
        else:
            self._api.add_key(combo, lambda _ctx, cmd=command: self._api.execute(cmd))
        return _SCRIPT_ERROR_OK

    def _add_trigger_ex(
        self,
        name: object = "",
        match: object = "",
        response: object = "",
        flags: object = 0,
        _colour: object = 0,
        _wildcard: object = 0,
        _sound_file: object = "",
        script_name: object = "",
        send_to: object = 0,
        sequence: object = 100,
        *_rest: object,
    ) -> int:
        """MUSHclient ``AddTriggerEx``: register a trigger at runtime.

        Erion's F-key report chain hangs off this: the hotkey alias sends "score hp"
        and AddTriggerEx's a OneShot+Replace trigger to speak the reply. One engine
        rule per *name*: a Replace re-registration with the same pattern just rewires
        the stored state (handler/flags), so hammering F1 doesn't stack a dead rule
        per press. OneShot deactivates itself on first fire.
        """
        flag_bits = int(_to_float(flags) or 0)
        trigger_name = str(name or "")
        match_text = str(match or "")
        if not match_text:
            return 1  # nonzero = MUSHclient error; packs rarely check
        regex = bool(flag_bits & _TRIGGER_FLAGS["RegularExpression"])
        pattern = match_text if regex else mushclient_pattern(match_text)
        if flag_bits & _TRIGGER_FLAGS["IgnoreCase"]:
            pattern = "(?i)" + pattern
        handler = self._lua.globals()[str(script_name)] if str(script_name or "") else None
        state = {
            "active": bool(flag_bits & _TRIGGER_FLAGS["Enabled"]),
            "one_shot": bool(flag_bits & _TRIGGER_FLAGS["OneShot"]),
            "handler": handler,
            "body": str(response or ""),
            "send_to": int(_to_float(send_to) or 0),
            "pattern": pattern,
            "regex": regex,
            "name": trigger_name,
            "retired": False,  # True once its engine rule has been removed (spent/deleted)
        }
        previous = self._dynamic_triggers.get(trigger_name) if trigger_name else None
        same_shape = previous is not None and (previous["pattern"], previous["regex"]) == (
            pattern, regex
        )
        if previous is not None and not previous["retired"] and same_shape:
            previous.update(state)  # same live rule: rewire in place, keep the one engine rule
            return _SCRIPT_ERROR_OK
        if previous is not None and not previous["retired"]:
            # Pattern changed on a still-live rule: retire the callback AND remove its engine
            # rule, or the dead pattern keeps getting scanned against every line all session.
            previous["active"] = False
            previous["retired"] = True
            self._api.remove_trigger(previous["engine_name"])
        engine_name = f"mush-dyn-{self._dyn_counter}"
        self._dyn_counter += 1
        state["engine_name"] = engine_name
        if trigger_name:
            self._dynamic_triggers[trigger_name] = state

        def fire(ctx: MatchContext) -> None:
            live = self._dynamic_triggers.get(trigger_name, state) if trigger_name else state
            if live is not state or not live["active"]:
                return  # replaced by a different rule shape, deleted, or disabled
            if live["one_shot"]:
                live["active"] = False
                live["retired"] = True
                # A spent one-shot is dead: drop its engine rule so churny packs (Erion's
                # "capture the next line" temporaries) don't accumulate dead rules per line.
                self._api.remove_trigger(live["engine_name"])
            if live["handler"] is not None:
                wildcards = self._lua.table_from(ctx.wildcards[1:])
                self._guard.run(live["handler"], live["name"], ctx.line.plain_text, wildcards)
            body = live["body"]
            if body:
                text = _substitute(body, ctx.wildcards)
                if live["send_to"] == int(_SEND_TO_SCRIPT):
                    self._guard.run(lambda: self._compile(text)())
                else:
                    self._api.send(text)

        self._api.add_trigger(
            pattern, fire, regex=True, priority=-int(_to_float(sequence) or 100),
            name=engine_name,
        )
        return _SCRIPT_ERROR_OK

    def _delete_trigger(self, name: object = "", *_rest: object) -> int:
        state = self._dynamic_triggers.pop(str(name or ""), None)
        if state is None:
            return 1  # unknown (or XML-defined) trigger: MUSHclient errors, packs shrug
        state["active"] = False
        state["retired"] = True
        self._api.remove_trigger(state["engine_name"])  # actually drop it, don't just deactivate
        return _SCRIPT_ERROR_OK

    def _enable_trigger(self, name: object = "", enabled: object = 1, *_rest: object) -> int:
        state = self._dynamic_triggers.get(str(name or ""))
        if state is not None:
            state["active"] = bool(_to_float(enabled))
            return _SCRIPT_ERROR_OK
        changed = self._enable_xml_rules("trigger", str(name or ""), enabled, by_group=False)
        return _SCRIPT_ERROR_OK if changed else 1

    def _enable_xml_rules(
        self, kind: str | None, token: str, enabled: object, *, by_group: bool
    ) -> bool:
        changed = False
        key = "group" if by_group else "name"
        for state in self._xml_rules:
            if (kind is None or state["kind"] == kind) and state[key] == token:
                state["enabled"] = bool(_to_float(enabled))
                changed = True
        return changed

    def _enable_trigger_group(self, group: object = "", enabled: object = 1, *_rest: object) -> int:
        changed = self._enable_xml_rules(
            "trigger", str(group or ""), enabled, by_group=True
        )
        return _SCRIPT_ERROR_OK if changed else 1

    def _enable_alias(self, name: object = "", enabled: object = 1, *_rest: object) -> int:
        changed = self._enable_xml_rules("alias", str(name or ""), enabled, by_group=False)
        return _SCRIPT_ERROR_OK if changed else 1

    def _enable_alias_group(self, group: object = "", enabled: object = 1, *_rest: object) -> int:
        changed = self._enable_xml_rules("alias", str(group or ""), enabled, by_group=True)
        return _SCRIPT_ERROR_OK if changed else 1

    def _enable_group(self, group: object = "", enabled: object = 1, *_rest: object) -> int:
        changed = self._enable_xml_rules(None, str(group or ""), enabled, by_group=True)
        return _SCRIPT_ERROR_OK if changed else 1

    def _call_plugin(self, plugin_id: object = "", func: object = "", *args: object) -> int:
        """MUSHclient ``CallPlugin``: invoke another plugin's exposed function.

        Resolved against the ppi Expose registry (the only cross-plugin surface we
        keep). An unknown target no-ops with a nonzero code -- Erion only CallPlugins
        its cosmetic messages-window plugin, which may not be loaded at all.
        """
        fn = self._exposed.get(str(plugin_id or ""), {}).get(str(func or ""))
        if fn is None:
            return 1
        self._guard.run(fn, *args)
        return _SCRIPT_ERROR_OK

    def _do_after_special(self, delay: float, code: str, sendto: object = _SEND_TO_SCRIPT) -> None:
        deferred = self._compile(str(code))
        self._api.add_timer(float(delay), lambda: self._guard.run(deferred))

    def _compile(self, code: str):
        """Host-side compile of a Lua chunk into a zero-arg callable."""
        return self._lua.eval(f"function()\n{code}\nend")

    # --- loading ---

    def load_file(self, path: str) -> None:
        # The world file's directory anchors GetInfo() sound paths: sounds sit beside the
        # world (often nested below the pack root that require/ resolves against).
        entry = Path(path).resolve()
        self._world_path = entry
        self._world_dir = entry.parent.as_posix()
        self._client_dir = Path(self._base_dir).resolve() if self._base_dir else entry.parent
        for parent in entry.parents:
            if parent.name.casefold() == "worlds":
                self._client_dir = parent.parent
                break
        self._load_path(entry)
        # MUSHclient raises this after its initial plugin list settles. Dependency-manager
        # plugins use it to load their declared requirements (Miriani-Next); optional plugins
        # are deliberately not loaded and are recorded below.
        self.dispatch("OnPluginListChanged")
        self._record_optional_plugins()

    def load_source(self, xml: str) -> None:
        # Strip only the XML declaration -- ElementTree rejects an encoding decl on a str.
        # Keep the DOCTYPE: MUSHclient plugins declare config entities in its internal
        # subset (<!ENTITY foo "...">) and reference them as &foo;, which ET expands. (An
        # earlier strip of the whole DOCTYPE corrupted that subset -> ParseError.)
        xml = re.sub(r"<\?xml[^>]*\?>", "", xml)
        xml = _sanitize_attr_markup(xml)  # MUSHclient regex attrs carry raw < (named groups)
        self._load_plugin(ET.fromstring(xml))

    def _load_path(self, path: Path) -> None:
        """Load one XML/Lua include while retaining its directory for relative children."""
        previous = self._current_document
        self._current_document = path
        try:
            text = path.read_text(encoding="latin-1", errors="ignore")
            # MUSHclient's `constants.lua` is often an XML <script> wrapper despite its
            # suffix. Sniff the content rather than trusting the extension.
            if text.lstrip().startswith("<"):
                self.load_source(text)
            else:
                self._guard.run_strict(self._execute_lua, text)
        finally:
            self._current_document = previous

    def _execute_lua(self, source: str) -> object:
        name = "@" + self._current_document.as_posix() if self._current_document else "mushclient"
        return self._lua.execute(source, name=name)

    def _index_pack_files(self) -> dict[str, list[Path]]:
        if self._file_index is not None:
            return self._file_index
        index: dict[str, list[Path]] = {}
        if self._base_dir:
            base = Path(self._base_dir).resolve()
            for path in sorted(base.rglob("*")):
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                if resolved.is_file() and resolved.is_relative_to(base):
                    index.setdefault(resolved.name.casefold(), []).append(resolved)
        self._file_index = index
        return index

    def _resolve_pack_file(self, filename: str) -> Path | None:
        """Resolve Windows-style, case-insensitive pack paths without escaping the pack."""
        if not self._base_dir or not filename:
            return None
        base = Path(self._base_dir).resolve()
        normalized = filename.replace("\\", "/")
        normalized = re.sub(r"^[A-Za-z]:", "", normalized)
        requested = Path(normalized)
        direct: list[Path] = []
        if requested.is_absolute():
            direct.append(requested)
        if self._current_document is not None:
            direct.append(self._current_document.parent / normalized)
        direct.append(base / normalized.lstrip("/"))
        for candidate in direct:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved.is_file() and resolved.is_relative_to(base):
                return resolved

        basename = Path(normalized).name.casefold()
        matches = self._index_pack_files().get(basename, [])
        wanted = tuple(part.casefold() for part in Path(normalized).parts if part not in ("/", "."))

        def rank(path: Path) -> tuple[int, str]:
            rel = tuple(part.casefold() for part in path.relative_to(base).parts)
            suffix_match = bool(wanted) and rel[-len(wanted):] == wanted
            return (0 if suffix_match else 1, path.as_posix())

        return min(matches, key=rank) if matches else None

    def _resolve_pack_directory(self, value: str) -> Path | None:
        """Resolve a possibly-not-yet-created directory while confining it to the pack."""
        if not self._base_dir or not value:
            return None
        base = Path(self._base_dir).resolve()
        normalized = re.sub(r"^[A-Za-z]:", "", value.replace("\\", "/"))
        requested = Path(normalized)
        candidate = requested if requested.is_absolute() else (self._client_dir or base) / requested
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        return resolved if resolved.is_relative_to(base) else None

    def _plugin_metadata(self, path: Path) -> tuple[str, str, str]:
        """Return (id, name, language) from a plugin XML file, or empty values."""
        try:
            xml = path.read_text(encoding="latin-1", errors="ignore")
            xml = re.sub(r"<\?xml[^>]*\?>", "", xml)
            root = ET.fromstring(_sanitize_attr_markup(xml))
        except (OSError, ET.ParseError):
            return "", "", ""
        plugin = next(root.iter("plugin"), None)
        if plugin is None:
            return "", "", ""
        return (
            plugin.get("id", ""),
            plugin.get("name", "") or path.stem,
            plugin.get("language", ""),
        )

    def _find_plugin_by_id(self, plugin_id: str) -> Path | None:
        if self._plugin_file_by_id is None:
            index: dict[str, Path] = {}
            seen: set[Path] = set()
            for paths in self._index_pack_files().values():
                for path in paths:
                    if path in seen or path.suffix.casefold() != ".xml":
                        continue
                    seen.add(path)
                    candidate_id, _name, _language = self._plugin_metadata(path)
                    if candidate_id:
                        index.setdefault(candidate_id, path)
            self._plugin_file_by_id = index
        return self._plugin_file_by_id.get(plugin_id)

    def _plugin_skip_reason(self, path: Path) -> str | None:
        replacement = _HOST_PLUGIN_REPLACEMENTS.get(path.name.casefold())
        if replacement:
            return replacement
        if path.suffix.lower() in _NON_LUA_SCRIPT_SUFFIXES:
            return f"requires unsupported {path.suffix[1:].upper()} scripting"
        _plugin_id, _name, language = self._plugin_metadata(path)
        if language and language.casefold() != "lua":
            return f"requires unsupported {language} scripting"
        return None

    def _mark_virtual_plugin(self, path: Path) -> None:
        plugin_id, name, _language = self._plugin_metadata(path)
        if plugin_id:
            self._plugin_info[plugin_id] = {
                "name": name,
                "path": path,
                "enabled": True,
            }

    def _skip_plugin(self, name: str, reason: str) -> None:
        item = (name, reason)
        if item not in self._skipped_plugins:
            self._skipped_plugins.append(item)

    def _record_optional_plugins(self) -> None:
        rawget = self._lua.eval("rawget")
        table = rawget(self._lua.globals(), "optional_plugins")
        if table is None:
            return
        try:
            names = [str(name) for _plugin_id, name in table.items()]
        except (AttributeError, TypeError):
            return
        loaded_names = {str(info["name"]).casefold() for info in self._plugin_info.values()}
        for name in sorted(names, key=str.casefold):
            if name.casefold() not in loaded_names:
                self._skip_plugin(name + ".xml", "declared optional by the pack")

    def _load_plugin(self, root: ET.Element) -> None:
        """Run one plugin/world's script + triggers; a world (<include>s) pulls in its
        plugins so they share this runtime and can ppi-message each other."""
        plugin = next(root.iter("plugin"), None)
        previous = self._current_plugin
        plugin_id = (plugin.get("id") if plugin is not None else "") or "world"
        self._current_plugin = plugin_id
        if plugin is not None and plugin_id != "world":
            plugin_name = plugin.get("name", "")
            if not plugin_name and self._current_document is not None:
                plugin_name = self._current_document.stem
            self._plugin_info[plugin_id] = {
                "name": plugin_name,
                "path": self._current_document,
                "enabled": True,
            }
        try:
            # Non-plugin includes are Lua/constants fragments and must exist before the
            # containing script executes. Plugin includes load afterward as independent
            # plugin units, matching MUSHclient's dependency order.
            for include in root.iter("include"):
                name = include.get("name")
                if name and include.get("plugin", "n").lower() != "y":
                    try:
                        self._load_included(name)
                    except Exception as exc:  # noqa: BLE001 - account for one bad fragment
                        self._include_errors.append((name, f"{type(exc).__name__}: {exc}"))
            world = next(root.iter("world"), None)
            if world is not None:
                self._world_id = world.get("id") or self._world_id
                self._world_name = world.get("name", self._world_name)
                self._world_address = world.get("site", self._world_address)
                try:
                    self._world_port = int(world.get("port", self._world_port))
                except (TypeError, ValueError):
                    pass
            script_filename = world.get("script_filename", "") if world is not None else ""
            if script_filename:
                target = self._resolve_pack_file(script_filename)
                if target is None:
                    self._skip_plugin(
                        script_filename, "external world script is not bundled with the pack"
                    )
                else:
                    try:
                        self._load_path(target)
                    except Exception as exc:  # noqa: BLE001 - XML rules can still be useful
                        self._external_script_errors.append(
                            (script_filename, f"{type(exc).__name__}: {exc}")
                        )
            script = "\n".join((el.text or "") for el in root.iter("script"))
            if script.strip():
                self._guard.run_strict(self._execute_lua, script)
            for is_alias, tag in ((False, "trigger"), (True, "alias")):
                for element in root.iter(tag):
                    try:
                        self._register(element, is_alias=is_alias)
                    except Exception as exc:  # noqa: BLE001 - one malformed rule is not the pack
                        label = element.get("name") or element.get("match") or "(unnamed)"
                        self._rule_errors.append((label, f"{type(exc).__name__}: {exc}"))
            self._capture_hooks()
        finally:
            self._current_plugin = previous
        for include in root.iter("include"):
            name = include.get("name")
            if not name or include.get("plugin", "n").lower() != "y":
                continue
            try:
                self._load_included(name)
            except Exception as exc:  # noqa: BLE001 - a malformed plugin must not sink the pack
                self._include_errors.append((name, f"{type(exc).__name__}: {exc}"))

    def _load_included(self, filename: str) -> None:
        target = self._resolve_pack_file(filename)
        if target is None:
            self._skip_plugin(filename, "not bundled with the pack")
            return
        if target in self._loaded_includes:  # dedup by file (dirs share names)
            return
        self._loaded_includes.add(target)
        reason = self._plugin_skip_reason(target)
        if reason:
            self._mark_virtual_plugin(target)
            self._skip_plugin(filename, reason)
            return
        self._load_path(target)

    # --- plugin lifecycle ---

    def _capture_hooks(self) -> None:
        """Claim the ``OnPlugin*`` functions the current plugin's script defined.

        Must use ``rawget``: the black-hole ``_G`` metatable reports every name as
        defined. Captured globals are cleared so the next plugin in the shared
        runtime neither inherits nor overwrites them (MUSHclient gives each plugin
        its own script space; this is the shared-``_G`` equivalent).
        """
        rawget = self._lua.eval("rawget")
        globals_ = self._lua.globals()
        captured = self._hooks.setdefault(self._current_plugin, {})
        for name in _LIFECYCLE_HOOKS:
            fn = rawget(globals_, name)
            if fn is not None:
                captured[name] = fn
                globals_[name] = None

    def dispatch(self, name: str, *args: object, caller: object | None = None) -> None:
        """Call one lifecycle hook on every plugin that defines it, in load order.

        Each call is time-budgeted and isolated: one plugin's failing hook is
        traced to the diagnostic log and the rest still run, mirroring MUSHclient
        where one erroring plugin doesn't halt the others. ``caller`` interposes a
        Lua-side adapter (``caller(fn, *args)``) for arguments that can't cross the
        lupa boundary as-is (byte payloads).
        """
        # A list-changed hook can load more plugins, mutating _hooks during dispatch.
        for plugin_id, hooks in list(self._hooks.items()):
            fn = hooks.get(name)
            if fn is None:
                continue
            previous = self._current_plugin
            self._current_plugin = plugin_id
            try:
                if caller is not None:
                    self._guard.run_strict(caller, fn, *args)
                else:
                    self._guard.run_strict(fn, *args)
            except Exception as exc:  # noqa: BLE001 - one plugin's hook must not stop the rest
                item = (f"{plugin_id}.{name}", f"{type(exc).__name__}: {exc}")
                if item not in self._hook_errors:
                    self._hook_errors.append(item)
                diag = self._api.diag
                if diag is not None:
                    diag.event("plugin.dispatch", hook=name, plugin=plugin_id,
                               error=f"{type(exc).__name__}: {exc}")
            finally:
                self._current_plugin = previous

    def has_hook(self, name: str) -> bool:
        """Whether any loaded plugin defined this lifecycle hook (drives tick arming)."""
        return any(hooks.get(name) is not None for hooks in self._hooks.values())

    def dispatch_install(self) -> None:
        """MUSHclient calls each plugin's ``OnPluginInstall`` at load; packs set their
        variable defaults there (Erion turns every sound toggle on), so skipping it
        leaves the pack loaded but gated silent."""
        self.dispatch("OnPluginInstall")

    def dispatch_connect(self) -> None:
        self.dispatch("OnPluginConnect")

    def dispatch_line(self, line: str) -> None:
        self.dispatch("OnPluginLineReceived", line)

    def dispatch_gmcp(self, package: str, value: object) -> None:
        """Expose parsed GMCP data and mirror GMCP_handler_NJG's plugin broadcast."""
        root = package.casefold()
        self._gmcp_values[root] = value

        def flatten(prefix: str, item: object) -> None:
            if not isinstance(item, dict):
                return
            for key, child in item.items():
                child_path = f"{prefix}.{str(key).casefold()}"
                self._gmcp_values[child_path] = child
                flatten(child_path, child)

        flatten(root, value)
        self.dispatch("OnPluginBroadcast", 1, _GMCP_HANDLER_ID, "GMCP_handler_NJG", root)

    def dispatch_telnet_request(self, option: int, message: str) -> None:
        """``OnPluginTelnetRequest(option, "WILL"/"SENT_DO")`` -- the SENT_DO round is
        where MSDP packs send their REPORT list; without it the server streams nothing."""
        self.dispatch("OnPluginTelnetRequest", option, message)

    def dispatch_telnet_subnegotiation(self, option: int, payload: bytes) -> None:
        # MUSHclient hands plugins the raw payload as a Lua byte-string. It crosses
        # into Lua as a byte table and is reassembled there (_payload_caller).
        table = self._lua.table_from(list(payload))
        self.dispatch(
            "OnPluginTelnetSubnegotiation", option, table, caller=self._payload_caller
        )

    def _register(self, element: ET.Element, *, is_alias: bool) -> None:
        attrs = element.attrib
        pattern = attrs.get("match", "")
        if attrs.get("regexp", "n") != "y":
            pattern = mushclient_pattern(pattern)
        if attrs.get("ignore_case", "n") == "y":
            pattern = "(?i)" + pattern  # MUSHclient compiles these with PCRE_CASELESS
        try:  # a malformed sequence attribute must not abort the whole world load
            priority = -int(attrs.get("sequence", _DEFAULT_TRIGGER_SEQUENCE))
        except ValueError:
            priority = -int(_DEFAULT_TRIGGER_SEQUENCE)
        keep_default = "n" if is_alias else "y"  # aliases consume by default
        keep = attrs.get("keep_evaluating", keep_default) == "y"
        callback = self._make_callback(element, attrs)
        state: dict[str, object] = {
            "kind": "alias" if is_alias else "trigger",
            "name": attrs.get("name", ""),
            "group": attrs.get("group", ""),
            "enabled": attrs.get("enabled", "y") == "y",
        }
        self._xml_rules.append(state)

        def gated(ctx: MatchContext) -> None:
            if state["enabled"]:
                callback(ctx)

        if is_alias:
            self._api.add_alias(
                pattern, gated, regex=True, priority=priority, keep_evaluating=keep
            )
        else:
            self._api.add_trigger(
                pattern, gated, regex=True, priority=priority, keep_evaluating=keep
            )

    def _make_callback(self, element: ET.Element, attrs: dict[str, str]):
        lua = self._lua
        api = self._api
        name = attrs.get("name", "")

        script_name = attrs.get("script")
        if script_name:
            handler = lua.globals()[script_name]

            def call_named(ctx: MatchContext) -> None:
                if handler is not None:
                    wildcards = lua.table_from(ctx.wildcards[1:])
                    self._guard.run(handler, name, ctx.line.plain_text, wildcards)

            return call_named

        send_element = element.find("send")
        body = (send_element.text or "") if send_element is not None else ""
        if body.strip():
            if attrs.get("send_to", "0") == _SEND_TO_SCRIPT:
                if _WILDCARD_RE.search(body):
                    # MUSHclient substitutes %1.. into send-to-script text per match, then
                    # runs it. Can't precompile: a bare %1 (e.g. `for i=1,%1`) isn't valid Lua.
                    def call_script(ctx: MatchContext) -> None:
                        # Compile inside the guard: a syntax error from substituted MUD text
                        # must be contained, not raised into line processing.
                        self._guard.run(lambda: self._compile(_substitute(body, ctx.wildcards))())
                else:
                    compiled = self._compile(body)  # no wildcards: compile once at registration

                    def call_script(_ctx: MatchContext) -> None:
                        self._guard.run(compiled)

                return call_script

            def call_send(ctx: MatchContext) -> None:
                api.send(_substitute(body, ctx.wildcards))

            return call_send

        return lambda _ctx: None


_CDATA_RE = re.compile(r"<!\[CDATA\[.*?\]\]>", re.DOTALL)
_ATTR_VALUE_RE = re.compile(r'="([^"]*)"')
_BARE_AMP_RE = re.compile(r"&(?!(?:[A-Za-z][\w.-]*|#\d+|#x[0-9A-Fa-f]+);)")


def _sanitize_attr_markup(xml: str) -> str:
    """Escape raw ``<`` and bare ``&`` inside double-quoted attribute values (outside CDATA).

    MUSHclient plugins put regexes with named groups in ``match="(?P<name>...)"`` attributes;
    the raw ``<`` is illegal XML and trips ElementTree even though MUSHclient tolerates it.
    Script bodies live in CDATA and are left untouched; well-formed packs have nothing to
    escape (entities already use ``&...;``), so this is a no-op for them.
    """
    out: list[str] = []
    last = 0
    for cdata in _CDATA_RE.finditer(xml):
        out.append(_escape_attr_values(xml[last : cdata.start()]))
        out.append(cdata.group(0))
        last = cdata.end()
    out.append(_escape_attr_values(xml[last:]))
    return "".join(out)


def _escape_attr_values(segment: str) -> str:
    # A few hand-edited packs omit the required whitespace between adjacent attributes
    # (`send_to="12"sequence="100"`). MUSHclient accepts that; ElementTree does not.
    segment = re.sub(r'"(?=[A-Za-z_:][\w:.-]*\s*=)', '" ', segment)

    def fix(match: re.Match[str]) -> str:
        value = _BARE_AMP_RE.sub("&amp;", match.group(1)).replace("<", "&lt;")
        return f'="{value}"'

    return _ATTR_VALUE_RE.sub(fix, segment)


def _substitute(text: str, wildcards: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return wildcards[index] if index < len(wildcards) else ""

    return _WILDCARD_RE.sub(replace, text)

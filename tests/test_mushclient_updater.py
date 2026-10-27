"""A pack's own updater plugin, answered by genericMud's updater.

Miriani-Next and Toastush each ship an updater.xml that genericMud doesn't load (it does the
updating itself). Their "update" commands, typed or sent by the packs, are answered here, and
after an install the pack hears what the plugin used to tell it.
"""

from __future__ import annotations

import json

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.packs.setup import GitUpdateResult
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from tests.helpers import RecordingSink

MIRIANI_NEXT_UPDATER = "706cd88f4d441a81439bd421"
TOASTUSH_UPDATER = "508bd88f4d441f81466bf471"

# A plugin of the pack that hears broadcasts, as Miriani-Next's toastush.xml does.
LISTENER = (
    '<muclient><plugin id="aaaaaaaaaaaaaaaaaaaaaaaa" name="listener" language="Lua"/>'
    "<script><![CDATA[\n"
    "heard = {}\n"
    "function OnPluginBroadcast(msg, id, name, text)\n"
    '  heard[#heard + 1] = msg .. "|" .. id .. "|" .. name .. "|" .. text\n'
    '  SetVariable("heard", table.concat(heard, "\\n"))\n'
    "end\n"
    "]]></script></muclient>"
)


def _world(tmp_path, updater_id=MIRIANI_NEXT_UPDATER, rules=""):
    """A world that includes an updater.xml (skipped: genericMud stands in for it) and a
    plugin listening for broadcasts."""
    worlds = tmp_path / "worlds"
    (worlds / "plugins").mkdir(parents=True)
    (worlds / "plugins" / "updater.xml").write_text(
        f'<muclient><plugin id="{updater_id}" name="updater" language="Lua"/>'
        '<aliases><alias match="^update( .*)?$" regexp="y" enabled="y" send_to="12">'
        "<send>Send('the real updater ran')</send></alias></aliases></muclient>",
        encoding="latin-1",
    )
    (worlds / "plugins" / "listener.xml").write_text(LISTENER, encoding="latin-1")
    (worlds / "w.MCL").write_text(
        '<muclient><world site="x" port="1" name="W"/>'
        '<include name="updater.xml" plugin="y"/>'
        '<include name="listener.xml" plugin="y"/>'
        f"{rules}</muclient>",
        encoding="latin-1",
    )
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    pack.load_file(str(worlds / "w.MCL"))
    return sink, engine, pack


def _heard(pack: MushclientPack) -> list[tuple[int, str, str, str]]:
    heard = pack._api.get_var("heard") or ""
    entries = []
    for entry in heard.split("\n") if heard else []:
        message, plugin_id, name, text = entry.split("|", 3)
        entries.append((int(message), plugin_id, name, text))
    return entries


def test_the_packs_automatic_update_installs_instead_of_going_to_the_mud(tmp_path):
    # Miriani-Next's hooks.lua, when the MUD says the pack is out of date and the player
    # turned automatic updates on. Skipping updater.xml sent "update quietly" to the MUD.
    sink, engine, _pack = _world(
        tmp_path,
        rules='<triggers><trigger match="^#\\$#soundpack status outdated$" regexp="y" '
        'enabled="y" send_to="12"><send>Execute("update quietly")</send></trigger></triggers>',
    )
    engine.process_line(Line("#$#soundpack status outdated"))
    assert sink.update_requests == [True]
    assert sink.sent == []


@pytest.mark.parametrize(
    ("typed", "requests", "checks"),
    [
        ("update", [False], 0),
        ("update quietly", [True], 0),
        ("update --quiet", [True], 0),  # Toastush at login
        ("update -q -f", [True], 0),  # Toastush while idle
        ("update -c -s", [False], 0),
        ("update check", [], 1),
        ("update -d", [], 1),
    ],
)
def test_update_commands_install_or_check(tmp_path, typed, requests, checks):
    sink, engine, _pack = _world(tmp_path)
    assert engine.process_input(typed) == []
    assert sink.update_requests == requests
    assert sink.update_checks == checks
    assert sink.sent == []


@pytest.mark.parametrize("typed", ["update install", "update switch dev", "update -V"])
def test_the_launchers_own_commands_get_a_word_instead(tmp_path, typed):
    sink, engine, _pack = _world(tmp_path)
    assert engine.process_input(typed) == []
    assert sink.update_requests == [] and sink.update_checks == 0
    assert any("genericMud updates this soundpack itself" in text for text, _ in sink.echoed)


def test_toastushs_index_command_gets_a_word_instead_of_going_to_the_mud(tmp_path):
    # Its updater makes the update index the maintainers publish with "index <file>". MUSHclient
    # catches that, so it never reached the MUD; with the updater stood in for, it went out.
    sink, engine, _pack = _world(tmp_path, updater_id=TOASTUSH_UPDATER)
    assert engine.process_input("Index pack-v5.manifest") == []
    assert sink.sent == []
    assert any("index" in text.lower() for text, _ in sink.echoed)


def test_index_is_left_alone_where_toastushs_updater_isnt(tmp_path):
    _sink, engine, _pack = _world(tmp_path)  # Miriani-Next's updater has no index command
    assert engine.process_input("index stars") == ["index stars"]


def test_update_help_says_what_the_command_does_here(tmp_path):
    sink, engine, _pack = _world(tmp_path)
    engine.process_input("update help")
    assert any("Type update to install" in text for text, _ in sink.echoed)
    assert sink.update_requests == []


def test_a_packs_own_update_alias_still_wins(tmp_path):
    sink, engine, _pack = _world(
        tmp_path,
        rules='<aliases><alias match="update" enabled="y"><send>update now</send></alias>'
        "</aliases>",
    )
    assert engine.process_input("update") == []
    assert sink.sent == ["update now"]
    assert sink.update_requests == []


def test_ctrl_u_updates_where_miriani_nexts_updater_bound_it(tmp_path):
    sink, engine, _pack = _world(tmp_path)
    assert engine.press_key("ctrl+u")
    assert sink.update_requests == [False]
    _sink, toastush, _pack = _world(tmp_path / "toastush", updater_id=TOASTUSH_UPDATER)
    assert not toastush.has_key("ctrl+u")  # Toastush's updater binds no key


def test_after_an_install_miriani_next_hears_what_its_updater_told_it(tmp_path):
    _sink, _engine, pack = _world(tmp_path)
    pack.pack_updated(GitUpdateResult(
        "Miriani-Next", False, written=2, removed=1,
        written_files=["sounds/a.ogg", "worlds/plugins/updater.xml"],
        removed_files=["lua/old.lua"],
    ))
    [(message, plugin_id, name, text)] = _heard(pack)
    assert (message, plugin_id, name) == (777, MIRIANI_NEXT_UPDATER, "updater")
    # The updater plugin itself is left out: genericMud never loads it.
    assert json.loads(text) == {
        "result": "success", "files": ["sounds/a.ogg", "lua/old.lua"], "channel": "stable",
    }


def test_up_to_date_plays_its_sound_only_when_not_asked_quietly(tmp_path):
    _sink, _engine, pack = _world(tmp_path)
    pack.pack_updated(GitUpdateResult("Miriani-Next", True), quiet=True)
    assert _heard(pack) == []
    pack.pack_updated(GitUpdateResult("Miriani-Next", True))
    assert _heard(pack) == [(999, MIRIANI_NEXT_UPDATER, "updater", "")]


def test_after_an_install_toastush_hears_its_updaters_event(tmp_path):
    _sink, _engine, pack = _world(tmp_path, updater_id=TOASTUSH_UPDATER)
    pack.pack_updated(GitUpdateResult(
        "Toastush", False, written=1, removed=1,
        written_files=["sounds/a.ogg"], removed_files=["lua/old.lua"],
    ))
    [(message, plugin_id, _name, text)] = _heard(pack)
    assert (message, plugin_id) == (1, TOASTUSH_UPDATER)
    assert json.loads(text) == {
        "create": [], "delete": [["lua/old.lua", {}]], "update": [["sounds/a.ogg", {}]],
    }


def test_a_pack_without_an_updater_plugin_hears_nothing_and_keeps_update_for_the_mud(tmp_path):
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    pack.load_source(LISTENER)
    pack.pack_updated(GitUpdateResult("Pack", False, written=1, written_files=["a.ogg"]))
    assert _heard(pack) == []
    assert engine.process_input("update") == ["update"]

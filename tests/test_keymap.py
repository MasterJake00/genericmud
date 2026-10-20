"""Keymap loader test."""

from __future__ import annotations

from genericmud.config.keymap import load_keymap
from genericmud.review.cursor import RECALL_MAX


def test_vipmud_keymap_loads_expected_bindings():
    keymap = load_keymap("vipmud")
    assert keymap["ctrl+1"] == "recall:1"
    assert keymap["ctrl+9"] == "recall:9"
    assert keymap["ctrl+0"] == "recall:10"
    assert keymap["alt+up"] == "review:prev_line"
    assert keymap["f11"] == "voice:flush"


def test_every_plain_recall_binding_is_within_the_cursors_cap():
    # A recall:N past RECALL_MAX fails silently -- recall() returns "" and nothing is
    # spoken -- so a binding and the cap have to move together. The per-channel forms
    # (alt+t/alt+c, and ctrl+alt+1-9's chan:recall:N) are a separate layer with their own
    # bound, so only "recall:<digits>" is checked here.
    keymap = load_keymap("vipmud")
    numbers = [
        int(action.split(":")[1])
        for action in keymap.values()
        if action.startswith("recall:") and action.split(":")[1].isdigit()
    ]
    assert numbers, "the default keymap should bind at least one recall"
    assert max(numbers) <= RECALL_MAX

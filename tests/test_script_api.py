"""ScriptApi path resolution: pack-dir anchoring + the Sounds-folder (@sppath) fallback."""

from __future__ import annotations

import os

from genericmud.automation.engine import AutomationEngine
from genericmud.scripting.api import ScriptApi
from tests.helpers import RecordingDiag, RecordingSink


def _resolves_to(resolved: str, expected) -> bool:
    """The resolved path points at the expected file.

    Windows resolution yields mixed separators and case (its filesystem accepts both), so
    compare by identity of the actual file rather than exact string form.
    """
    return os.path.samefile(resolved, expected)


def _api(base_dir: str) -> ScriptApi:
    return ScriptApi(AutomationEngine(RecordingSink()), base_dir=base_dir)


def _use_sounds_folder(api: ScriptApi, folder) -> None:
    """Point @sppath at the player's own sounds folder, as the window does from the world's
    settings: the variable, and the engine's record that the folder is the player's."""
    api.set_var("sppath", str(folder))
    api._engine.sounds_dir = str(folder)


def test_resolve_keeps_existing_pack_path(tmp_path):
    sound = tmp_path / "beep.wav"
    sound.write_bytes(b"RIFF")
    assert _api(str(tmp_path))._resolve("beep.wav")[0] == str(sound)  # exists -> used as-is


def test_resolve_falls_back_to_sounds_folder_by_basename(tmp_path):
    pack, sounds = tmp_path / "pack", tmp_path / "mysounds"
    pack.mkdir()
    (sounds / "fx").mkdir(parents=True)
    real = sounds / "fx" / "boom.wav"
    real.write_bytes(b"RIFF")
    api = _api(str(pack))
    _use_sounds_folder(api, sounds)
    # The pack points at <pack>/boom.wav (missing); the Sounds folder rescues it by basename.
    assert api._resolve("boom.wav")[0] == str(real)


def test_resolve_tolerates_an_upstream_doubled_extension(tmp_path):
    pack, sounds = tmp_path / "pack", tmp_path / "sounds"
    pack.mkdir()
    sounds.mkdir()
    real = sounds / "CrossbowFire2.wav.wav"
    real.write_bytes(b"RIFF")
    api = _api(str(pack))
    _use_sounds_folder(api, sounds)
    resolved, exists = api._resolve("Cogg\\Combat\\CrossbowFire2.wav")
    assert exists
    assert resolved == str(real)


def test_resolve_finds_windows_authored_path_by_basename(tmp_path):
    pack, sounds = tmp_path / "pack", tmp_path / "snd"
    pack.mkdir()
    sounds.mkdir()
    (sounds / "boom.wav").write_bytes(b"RIFF")
    api = _api(str(pack))
    _use_sounds_folder(api, sounds)
    # A MUSHclient/VIPMud pack may reference sounds with backslashes; the leaf must still
    # resolve via the Sounds folder on Linux, where "\" isn't a path separator.
    assert api._resolve("sub\\boom.wav")[0] == str(sounds / "boom.wav")


def test_resolve_returns_expected_path_when_unfound(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    api = _api(str(pack))
    api.set_var("sppath", str(tmp_path / "nope"))  # not a directory
    # Missing everywhere: return the expected path so the diagnostic can report where it looked.
    # Compare via Path so the separator matches the host (os.path.join emits "\" on Windows).
    assert api._resolve("ghost.wav")[0] == str(pack / "ghost.wav")


def test_resolve_without_sppath_is_unchanged(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    assert _api(str(pack))._resolve("ghost.wav")[0] == str(pack / "ghost.wav")


def test_resolve_traces_the_sppath_fallback(tmp_path):
    pack, sounds = tmp_path / "pack", tmp_path / "snd"
    pack.mkdir()
    sounds.mkdir()
    (sounds / "boom.wav").write_bytes(b"RIFF")
    engine = AutomationEngine(RecordingSink())
    engine.diag = RecordingDiag()
    api = ScriptApi(engine, base_dir=str(pack))
    _use_sounds_folder(api, sounds)
    api._resolve("boom.wav")  # missing in pack, rescued from the Sounds folder
    fields = engine.diag.fields("play.resolve")
    assert fields["fallback"] == "sppath" and fields["exists"] is True


def test_resolve_traces_a_total_miss(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    engine = AutomationEngine(RecordingSink())
    engine.diag = RecordingDiag()
    ScriptApi(engine, base_dir=str(pack))._resolve("ghost.wav")  # nowhere
    fields = engine.diag.fields("play.resolve")
    assert fields["exists"] is False and fields["fallback"] == "none"


def test_resolve_backslash_subdir_keeps_its_directory(tmp_path):
    # "social\hit.wav" is ONE filename on POSIX; the join must normalize separators so
    # the pack's own copy resolves, not whichever same-named leaf a fallback walk met.
    pack = tmp_path / "pack"
    (pack / "combat").mkdir(parents=True)
    (pack / "social").mkdir()
    (pack / "combat" / "hit.wav").write_bytes(b"RIFF")
    real = pack / "social" / "hit.wav"
    real.write_bytes(b"RIFF")
    assert _resolves_to(_api(str(pack))._resolve("social\\hit.wav")[0], real)


def test_sounds_folder_fallback_prefers_the_relative_path_over_the_basename(tmp_path):
    pack, sounds = tmp_path / "pack", tmp_path / "snd"
    pack.mkdir()
    (sounds / "combat").mkdir(parents=True)
    (sounds / "social").mkdir()
    (sounds / "combat" / "hit.wav").write_bytes(b"RIFF")
    real = sounds / "social" / "hit.wav"
    real.write_bytes(b"RIFF")
    api = _api(str(pack))
    _use_sounds_folder(api, sounds)
    # Wrong-case, backslash-separated, and missing from the pack dir: the relative
    # index must still pick social/, not combat/'s identically-named leaf.
    assert _resolves_to(api._resolve("Social\\Hit.wav")[0], real)


def test_sounds_folder_fallback_reduces_an_absolute_request_to_its_relative_form(tmp_path):
    # The dialects pre-resolve @sppath refs to absolute paths; a case-mismatched
    # directory must resolve via the relative index instead of basename roulette.
    pack, sounds = tmp_path / "pack", tmp_path / "snd"
    pack.mkdir()
    (sounds / "comm").mkdir(parents=True)
    (sounds / "misc").mkdir()
    (sounds / "misc" / "beep.wav").write_bytes(b"RIFF")
    real = sounds / "comm" / "beep.wav"
    real.write_bytes(b"RIFF")
    api = _api(str(pack))
    _use_sounds_folder(api, sounds)
    assert _resolves_to(api._resolve(str(sounds) + "/Comm/Beep.wav")[0], real)


def test_a_unc_sppath_is_never_resolved_while_looking_for_sounds(tmp_path, monkeypatch):
    # On Windows, resolving \\host\share sends the player's NTLM hash to that host.
    import genericmud.scripting.api as api_module

    looked_at: list[str] = []
    real_within = api_module.within

    def recording_within(root, candidate):
        looked_at.append(str(candidate))
        return real_within(root, candidate)

    monkeypatch.setattr(api_module, "within", recording_within)
    engine = AutomationEngine(RecordingSink())
    engine.sounds_dir = str(tmp_path)
    engine.set_var("sppath", "\\\\evil.example\\share")
    api = ScriptApi(engine, base_dir=str(tmp_path / "pack"))
    assert api._sounds_root() == str(tmp_path)
    assert not any("evil.example" in path for path in looked_at)

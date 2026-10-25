"""Credential store + heuristic auto-login (name/password prompt answering)."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.protocol.telnet import DataReceived
from genericmud.session.credentials import PlaintextCredentialStore
from genericmud.session.login import AutoLogin
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


def test_credential_store_roundtrip_and_persist(tmp_path):
    path = tmp_path / "c.json"
    store = PlaintextCredentialStore(path)
    assert store.get("gw") is None
    store.set("gw", "hero", "secret")
    assert store.get("gw") == ("hero", "secret")
    assert PlaintextCredentialStore(path).get("gw") == ("hero", "secret")  # persisted to disk
    store.delete("gw")
    assert store.get("gw") is None


def test_credential_store_ignores_malformed_rows(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(
        '{"number": 7, "missing": {"username": "hero"}, '
        '"wrong": {"username": 1, "password": false}}',
        encoding="utf-8",
    )
    store = PlaintextCredentialStore(path)
    assert store.get("number") is None
    assert store.get("missing") is None
    assert store.get("wrong") is None


def test_autologin_password_only_sent_after_username():
    sent: list[str] = []
    login = AutoLogin("hero", "secret", sent.append)
    login.feed("the password vault is locked")  # 'password' mentioned pre-login
    assert sent == []  # username not sent yet, so this is ignored
    login.feed("What is your name?")
    assert sent == ["hero"]
    login.feed("Password:")
    assert sent == ["hero", "secret"]
    assert login.done


def test_autologin_sends_each_prompt_once():
    sent: list[str] = []
    login = AutoLogin("hero", "secret", sent.append)
    login.feed("enter your name")
    login.feed("enter your name")  # a repeated prompt does not resend
    assert sent == ["hero"]


def test_autologin_ignores_password_word_in_a_sentence():
    sent: list[str] = []
    login = AutoLogin("hero", "secret", sent.append)
    login.feed("What is your name?")
    assert sent == ["hero"]
    login.feed("Never share your password with anyone.")  # a banner, not a prompt
    login.feed("Welcome, hero!")
    assert sent == ["hero"]  # the password was NOT leaked as a command
    assert not login.done


def test_autologin_window_expires_after_name():
    sent: list[str] = []
    login = AutoLogin("hero", "secret", sent.append, max_lines=3)
    login.feed("Enter your name:")
    assert sent == ["hero"]
    for _ in range(3):
        login.feed("just some game output")
    assert login.done  # stopped watching
    login.feed("Password:")  # arrives too late to matter
    assert sent == ["hero"]


def test_autologin_recognizes_real_password_prompt_variants():
    for prompt in ("Password:", "password >", "Your passphrase?", "PASSWORD"):
        sent: list[str] = []
        login = AutoLogin("hero", "secret", sent.append)
        login.feed("what is your name")
        login.feed(prompt)
        assert sent == ["hero", "secret"], prompt


def _app(store):
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    app = EngineApp(voice, send=sent.append, post=[].append, credentials=store, keymap={})
    return app, sent


def test_autologin_answers_name_then_password(tmp_path):
    store = PlaintextCredentialStore(tmp_path / "c.json")
    store.set("gw", "hero", "secret")
    app, sent = _app(store)
    app.begin_login("gw")
    app.on_telnet_event(DataReceived(b"What is your name?\r\n"))
    assert sent == ["hero"]
    app.on_telnet_event(DataReceived(b"Password:\r\n"))
    assert sent == ["hero", "secret"]


def test_autologin_not_armed_without_credentials(tmp_path):
    app, sent = _app(PlaintextCredentialStore(tmp_path / "c.json"))  # empty store
    app.begin_login("gw")
    app.on_telnet_event(DataReceived(b"What is your name?\r\n"))
    assert sent == []


def test_on_connect_arms_login(tmp_path):
    store = PlaintextCredentialStore(tmp_path / "c.json")
    store.set("gw", "hero", "secret")
    app, sent = _app(store)
    app.on_connect("gw")  # packs=None, so this just arms login
    app.on_telnet_event(DataReceived(b"Enter your name: \r\n"))
    assert sent == ["hero"]


def test_autologin_name_watch_gives_up_after_a_budget():
    # If the name prompt never matches (misconfigured/wrong world), stop watching instead
    # of lurking all session -- else a later in-game "by what name..." line sends the name.
    sent: list[str] = []
    login = AutoLogin("hero", "secret", sent.append, max_lines_to_user=5)
    for _ in range(5):
        login.feed("some banner or menu line")
    assert login.done
    login.feed("By what name do the villagers call you?")  # 500 lines into play
    assert sent == []


def test_autologin_disarms_on_user_input(tmp_path):
    store = PlaintextCredentialStore(tmp_path / "c.json")
    store.set("gw", "hero", "secret")
    app, sent = _app(store)
    app.begin_login("gw")
    app._dispatch_command("look")  # the user is driving login/play themselves
    app.on_telnet_event(DataReceived(b"What is your name?\r\n"))
    assert "hero" not in sent  # auto-login stood down


def test_password_input_is_masked_in_the_log_while_server_echoes(tmp_path):
    from genericmud.protocol import telnet as T

    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    app = EngineApp(voice, send=sent.append, post=[].append, keymap={},
                    name="gw", log_dir=tmp_path)
    app._toggle_log()  # start logging
    app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))  # server takes over echo
    app._dispatch_command("hunter2")  # the password
    app.on_telnet_event(T.Negotiation(T.WONT, T.OPT_ECHO))  # server releases echo
    app._dispatch_command("look")
    log_text = next(app.log_dir.glob("gw-*.log")).read_text(encoding="utf-8")
    assert "hunter2" not in log_text
    assert "> ***" in log_text
    assert "> look" in log_text  # normal input still logged in the clear


def _app_at_a_password_prompt():
    """An engine whose server has just taken over echo, the way it does for a password."""
    from genericmud.protocol import telnet as T

    sent: list[str] = []
    posted: list[dict] = []
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    app = EngineApp(voice, send=sent.append, post=posted.append, keymap={})
    app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))
    return app, sent, posted


def test_auto_repeat_never_swaps_the_password_for_the_last_command():
    from genericmud.protocol import telnet as T

    sent: list[str] = []
    voice = VoiceRouter(RecordingBackend(), clock=lambda: 0.0)
    app = EngineApp(voice, send=sent.append, post=[].append, keymap={})
    app.autoretype = True
    app.on_ws_message({"type": "input", "text": "hero"})  # the username, typed in the clear
    app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))
    app.on_ws_message({"type": "input", "text": "hunter2"})
    assert sent == ["hero", "hunter2"]


def test_a_password_goes_out_exactly_as_typed():
    app, sent, _posted = _app_at_a_password_prompt()
    aliased: list[str] = []
    app.engine.add_alias("hunter*", lambda ctx: aliased.append(ctx.line.plain_text))
    for password in ("abc;def", ".2news", "..3n", "hunter2", "n"):
        app.on_ws_message({"type": "input", "text": password})
    assert sent == ["abc;def", ".2news", "..3n", "hunter2", "n"]
    assert aliased == []
    assert app.nav.trail == []  # a password that spells a direction is not a step taken


def test_typed_input_goes_back_through_the_pipeline_once_the_server_echoes_again():
    from genericmud.protocol import telnet as T

    app, sent, _posted = _app_at_a_password_prompt()
    app.on_telnet_event(T.Negotiation(T.WONT, T.OPT_ECHO))
    app.on_ws_message({"type": "input", "text": "look;.2n"})
    assert sent == ["look", "n", "n"]


def test_the_renderer_hears_when_input_is_hidden_and_when_it_is_not():
    from genericmud.bridge import protocol
    from genericmud.protocol import telnet as T

    app, _sent, posted = _app_at_a_password_prompt()
    app.on_telnet_event(T.Negotiation(T.WILL, T.OPT_ECHO))  # a repeat changes nothing
    app.on_telnet_event(T.Negotiation(T.WONT, T.OPT_ECHO))
    flips = [message for message in posted if message["type"] == protocol.INPUT_MASKED]
    assert flips == [protocol.input_masked(True), protocol.input_masked(False)]


def test_a_dropped_connection_takes_the_hidden_input_with_it():
    from genericmud.bridge import protocol

    app, sent, posted = _app_at_a_password_prompt()
    app.on_connection_status("connection lost; reconnecting in 2s (attempt 1)")
    app.on_ws_message({"type": "input", "text": "look;look"})
    assert sent == ["look", "look"]  # split again: it's an ordinary command now
    assert posted.count(protocol.input_masked(False)) == 1


def test_the_server_asking_us_to_echo_leaves_a_password_hidden():
    # DO/DONT ECHO is about whether the CLIENT echoes. Treating DO like WONT unhid the
    # password: it was split on the separator, logged and kept for auto-repeat.
    from genericmud.protocol import telnet as T

    app, sent, _posted = _app_at_a_password_prompt()
    app.on_telnet_event(T.Negotiation(T.DO, T.OPT_ECHO))
    app.on_telnet_event(T.Negotiation(T.DONT, T.OPT_ECHO))
    app.on_ws_message({"type": "input", "text": "abc;def"})
    assert sent == ["abc;def"]


def _server_lines(app, count):
    from genericmud.protocol.telnet import DataReceived

    for number in range(count):
        app.on_telnet_event(DataReceived(f"Line {number} of the welcome text.\r\n".encode()))


def test_chat_while_a_password_prompt_is_open_does_not_unhide_it():
    # Changing a password on a busy LPMud: old password typed, then channel chatter and
    # combat keep arriving while the new-password prompt waits. Output says nothing about
    # whether the prompt is still open, so it must not end the hiding.
    app, sent, _posted = _app_at_a_password_prompt()
    app.on_ws_message({"type": "input", "text": "old;pass"})
    _server_lines(app, 60)
    app.on_ws_message({"type": "input", "text": "new;pass"})
    assert sent == ["old;pass", "new;pass"]


def test_a_server_that_hides_typing_for_good_lets_go_after_a_few_commands():
    # A server that holds WILL ECHO for the whole session would otherwise turn every
    # command into a password: no aliases, speedwalks, separator or /commands, ever.
    from genericmud.bridge import protocol

    app, sent, posted = _app_at_a_password_prompt()
    for answer in range(8):  # name, password, and a few commands that went out as typed
        app.on_ws_message({"type": "input", "text": f"answer{answer}"})
    app.on_ws_message({"type": "input", "text": "look;look"})
    assert sent[-2:] == ["look", "look"]
    assert posted[-1] == protocol.input_masked(False)  # Up-arrow history works again too


def test_a_retyped_password_stays_hidden_through_a_few_prompts():
    # ROM-style new character: one WILL ECHO, then the password, the retype, and a retry
    # loop if they don't match. A few prompt lines in between must not let go.
    app, sent, _posted = _app_at_a_password_prompt()
    app.on_ws_message({"type": "input", "text": "abc;def"})
    _server_lines(app, 2)  # "Passwords don't match." "Retype password:"
    app.on_ws_message({"type": "input", "text": "abc;deg"})
    assert sent == ["abc;def", "abc;deg"]

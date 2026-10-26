import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(
  new URL("../frontend/src/input.js", import.meta.url),
  "utf8",
);
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const { Input } = await import(moduleUrl);

function makeInput() {
  const sent = [];
  const input = Object.create(Input.prototype);
  input.element = {
    value: "",
    selectionStart: 0,
    setSelectionRange(start) {
      this.selectionStart = start;
    },
  };
  input.bridge = { send: (message) => sent.push(message) };
  input.history = [];
  input.historyIndex = 0;
  input.historyDraft = "";
  const announced = [];
  input.announce = (text) => announced.push(text);
  input.words = [];
  input.completion = null;
  return { input, sent, announced };
}

function keyEvent(key, modifiers = {}) {
  return {
    key,
    ctrlKey: false,
    altKey: false,
    shiftKey: false,
    metaKey: false,
    getModifierState: () => false,
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
    ...modifiers,
  };
}

test("Escape reaches the engine so it can stop hiding typed input", () => {
  const { input, sent } = makeInput();
  const event = keyEvent("Escape");

  input._onKey(event);

  assert.equal(event.prevented, true);
  assert.deepEqual(sent, [{ type: "key", key: "escape" }]);
});

test("modified Enter reaches the engine", () => {
  const { input, sent } = makeInput();
  const event = keyEvent("Enter", { ctrlKey: true });

  input._onKey(event);

  assert.equal(event.prevented, true);
  assert.deepEqual(sent, [{ type: "key", key: "ctrl+enter" }]);
});

test("Ctrl+Space completes from recent output and cycles forward", () => {
  const { input, sent } = makeInput();
  input.addOutput("A goblin arrives");
  input.addOutput("A goblin-king arrives");
  input.element.value = "kill gob";
  input.element.selectionStart = input.element.value.length;
  const event = keyEvent(" ", { ctrlKey: true });

  input._onKey(event);

  assert.equal(event.prevented, true);
  assert.equal(input.element.value, "kill goblin-king");
  input._onKey(keyEvent(" ", { ctrlKey: true }));
  assert.equal(input.element.value, "kill goblin");
  assert.deepEqual(sent, []);
});

test("Ctrl+Shift+Space can start completion in reverse", () => {
  const { input } = makeInput();
  input.addOutput("goblin gnome goblin-king");
  input.element.value = "g";
  input.element.selectionStart = 1;

  input._onKey(keyEvent(" ", { ctrlKey: true, shiftKey: true }));

  assert.equal(input.element.value, "goblin");
});

test("browser editing shortcuts remain local", () => {
  const { input, sent } = makeInput();
  const event = keyEvent("v", { ctrlKey: true });

  input._onKey(event);

  assert.equal(event.prevented, false);
  assert.deepEqual(sent, []);
});

test("macOS Command shortcuts remain local", () => {
  const { input, sent } = makeInput();
  const event = keyEvent("v", { metaKey: true });

  input._onKey(event);

  assert.equal(event.prevented, false);
  assert.deepEqual(sent, []);
});

test("AltGraph text entry remains local", () => {
  const { input, sent } = makeInput();
  const event = keyEvent("@", {
    ctrlKey: true,
    altKey: true,
    getModifierState: (modifier) => modifier === "AltGraph",
  });

  input._onKey(event);

  assert.equal(event.prevented, false);
  assert.deepEqual(sent, []);
});

test("browser find shortcuts remain available", () => {
  const { input, sent } = makeInput();
  const ctrlF = keyEvent("f", { ctrlKey: true });
  const f3 = keyEvent("F3");

  input._onKey(ctrlF);
  input._onKey(f3);

  assert.equal(ctrlF.prevented, false);
  assert.equal(f3.prevented, false);
  assert.deepEqual(sent, []);
});

test("history recall parks and restores the unsent draft", () => {
  const { input } = makeInput();
  input.history = ["look"];
  input.historyIndex = 1;
  input.element.value = "tell bob hel";  // typed but unsent
  input._recallHistory(-1);              // Up -> browse history
  assert.equal(input.element.value, "look");
  input._recallHistory(1);               // Down -> back to the draft, not blank
  assert.equal(input.element.value, "tell bob hel");
});

test("completion replaces the whole word when the caret is mid-word", () => {
  const { input } = makeInput();
  input.addOutput("sword");
  input.element.value = "get swoxx";
  input.element.selectionStart = 6;  // caret after "swo", before "xx"
  input._onKey(keyEvent(" ", { ctrlKey: true }));
  assert.equal(input.element.value, "get sword");  // not "get swordxx"
});

test("completion announces the word and no-match to the screen reader", () => {
  const { input, announced } = makeInput();
  input.addOutput("goblin");
  input.element.value = "kill gob";
  input.element.selectionStart = 8;
  input._onKey(keyEvent(" ", { ctrlKey: true }));
  assert.deepEqual(announced, ["goblin"]);
  input.completion = null;
  input.element.value = "kill zzz";
  input.element.selectionStart = 8;
  input._onKey(keyEvent(" ", { ctrlKey: true }));
  assert.deepEqual(announced, ["goblin", "no match for zzz"]);
});

test("a password typed while the server hides input stays out of history", () => {
  const { input, sent } = makeInput();
  input.setMasked(true);
  input.element.value = "hunter2";
  input._submit();
  input.setMasked(false);
  input.element.value = "look";
  input._submit();

  assert.deepEqual(sent, [
    { type: "input", text: "hunter2" },
    { type: "input", text: "look" },
  ]);
  assert.deepEqual(input.history, ["look"]);
});

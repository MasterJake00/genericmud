// Wires the renderer together: connect the bridge, route engine messages to the
// output/audio/status modules, and forward input/keys back to the engine.

import { Bridge } from "./ws.js";
import { Output } from "./output.js";
import { Input } from "./input.js";
import { Audio } from "./audio.js";
import { Status } from "./status.js";

const params = new URLSearchParams(location.search);
const port = params.get("port") || "8731";

const outputEl = document.getElementById("output");
const inputEl = document.getElementById("input");
const statusEl = document.getElementById("status");
const announceEl = document.getElementById("sr-announce");

const output = new Output(outputEl);
// onError reaches `bridge` lazily: it only fires on a later sound failure, by which
// point the const below is assigned. Errors land in the engine's diagnostic trace.
const audio = new Audio((e) =>
  bridge.send({ type: "client_error", scope: "audio", file: e.file, error: e.error }));
const status = new Status(statusEl);
let input;

const handlers = {
  line: (m) => {
    output.addLine(m);
    input.addOutput(m.text);
  },
  echo: (m) => output.addLine({ text: m.text }),
  sound: (m) => audio.play(m),
  stop_sound: (m) => audio.stop(m.channel),
  // Keep the engine-specified channel (default "music"); hardcoding it collapsed a pack's
  // custom music channels onto one bus and made stop_sound(<custom>) unable to stop them.
  music: (m) => audio.play({ ...m, loop: true, channel: m.channel || "music" }),
  status: (m) => status.update(m.gauges),
  review: () => { /* speech is native; visual reflection can be added later */ },
  connected: (m) => { announceEl.textContent = `Connected to ${m.world || "server"}`; },
  disconnected: () => { announceEl.textContent = "Disconnected"; },
  input_masked: (m) => input.setMasked(m.masked),
};

const bridge = new Bridge(port, handlers);
input = new Input(inputEl, bridge, (text) => { announceEl.textContent = text; });
inputEl.focus();

// Browser autoplay policy: the audio context starts suspended until a gesture.
document.addEventListener("keydown", () => {
  if (audio.ctx.state === "suspended") audio.ctx.resume();
}, { once: true });

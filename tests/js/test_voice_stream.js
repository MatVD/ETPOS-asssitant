const assert = require("node:assert/strict");
const {
  ETPOSVoiceStream,
  VoiceStreamError,
  finalInsertionDecision,
} = require("../../src/etpos_assistant/static/js/voice-stream.js");

class FakeSocket {
  constructor(mode = "ready") {
    this.mode = mode;
    this.readyState = 0;
    this.bufferedAmount = 0;
    this.sent = [];
    this.listeners = new Map();
    queueMicrotask(() => {
      this.readyState = 1;
      this.emit("open", {});
    });
  }

  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(callback);
  }

  emit(name, event) {
    for (const callback of this.listeners.get(name) || []) callback(event);
  }

  send(payload) {
    this.sent.push(payload);
    if (typeof payload !== "string") return;
    const message = JSON.parse(payload);
    if (message.type === "init") {
      queueMicrotask(() => {
        if (this.mode === "busy") {
          this.emit("message", {
            data: JSON.stringify({
              type: "error",
              code: "busy",
              message: "Une autre dictée est déjà en cours.",
            }),
          });
          return;
        }
        this.emit("message", {
          data: JSON.stringify({
            type: "ready",
            version: 1,
            dictation_id: "d1",
            limits: {
              max_samples: 32000,
              max_message_bytes: 16384,
              nominal_chunk_bytes: 8000,
              max_queue_messages: 4,
              finalization_timeout_seconds: 120,
            },
          }),
        });
      });
    } else if (message.type === "finish") {
      queueMicrotask(() => {
        this.emit("message", {
          data: JSON.stringify({
            type: "final",
            dictation_id: "d1",
            text: "texte final",
            covered_samples: message.total_samples,
          }),
        });
      });
    }
  }

  close() {
    this.readyState = 3;
    this.emit("close", {});
  }
}

function fakeStream(stopped) {
  return {
    getTracks() {
      return [{ stop() { stopped.count += 1; } }];
    },
  };
}

function makeAudioEnvironment({ onFlush } = {}) {
  const created = { node: null, source: null, context: null };

  class FakeAudioContext {
    constructor(options) {
      this.sampleRate = options.sampleRate;
      this.state = "running";
      this.audioWorklet = {
        addModule: async () => {},
      };
      created.context = this;
    }

    createMediaStreamSource() {
      const source = {
        connectedTo: null,
        connect(node) { this.connectedTo = node; },
        disconnect() {},
      };
      created.source = source;
      return source;
    }

    async resume() {}
    async close() { this.state = "closed"; }
  }

  class FakeAudioWorkletNode {
    constructor(_context, _name, options) {
      this.options = options;
      this.port = {
        onmessage: null,
        postMessage: (message) => {
          if (message.type !== "flush") return;
          onFlush?.(this);
          queueMicrotask(() => {
            this.port.onmessage?.({
              data: { type: "flushed", requestId: message.requestId },
            });
          });
        },
      };
      created.node = this;
    }

    disconnect() {}
  }

  return {
    created,
    FakeAudioContext,
    FakeAudioWorkletNode,
  };
}

async function testFinalInsertionDecision() {
  const snapshot = { value: "Question ", start: 9, end: 9 };
  assert.deepEqual(
    finalInsertionDecision(snapshot, "Question ", "texte final"),
    {
      mode: "insert",
      text: "texte final",
      selection: { start: 9, end: 9 },
    },
  );
  assert.deepEqual(
    finalInsertionDecision(snapshot, "Question modifiée", "texte final"),
    {
      mode: "review",
      text: "texte final",
      selection: null,
    },
  );
  assert.deepEqual(
    finalInsertionDecision(snapshot, "Question ", "   "),
    {
      mode: "none",
      text: "",
      selection: null,
    },
  );
}

async function testSupportDetection() {
  assert.equal(ETPOSVoiceStream.supported({}), false);
  assert.equal(ETPOSVoiceStream.supported({
    isSecureContext: true,
    WebSocket: class {},
    AudioContext: class {},
    AudioWorkletNode: class {},
    navigator: { mediaDevices: { getUserMedia() {} } },
  }), true);
}

async function testDoubleStartIsRejected() {
  const stopped = { count: 0 };
  const socket = new FakeSocket("ready");
  const audio = makeAudioEnvironment();
  const controller = new ETPOSVoiceStream({
    csrfToken: "csrf",
    websocketFactory: () => socket,
    mediaDevices: {
      async getUserMedia() {
        return fakeStream(stopped);
      },
    },
    AudioContextClass: audio.FakeAudioContext,
    AudioWorkletNodeClass: audio.FakeAudioWorkletNode,
    location: { protocol: "https:", host: "example.test" },
  });

  await controller.start();
  await assert.rejects(
    controller.start(),
    (error) => error instanceof VoiceStreamError && error.code === "invalid_state",
  );
  await controller.cancel();
}

async function testMicrophoneRefusalReturnsToIdle() {
  const socket = new FakeSocket("ready");
  const errors = [];
  const audio = makeAudioEnvironment();
  const refusal = new Error("denied");
  refusal.name = "NotAllowedError";
  const controller = new ETPOSVoiceStream({
    csrfToken: "csrf",
    websocketFactory: () => socket,
    mediaDevices: {
      async getUserMedia() {
        throw refusal;
      },
    },
    AudioContextClass: audio.FakeAudioContext,
    AudioWorkletNodeClass: audio.FakeAudioWorkletNode,
    location: { protocol: "https:", host: "example.test" },
    onError: (error) => errors.push(error),
  });

  await assert.rejects(controller.start());
  assert.equal(controller.state, "idle");
  assert.equal(errors.length, 1);
  assert.match(errors[0].message, /microphone refusé/i);
}

async function testServerRefusalPreventsMicrophoneCapture() {
  let mediaCalls = 0;
  const socket = new FakeSocket("busy");
  const controller = new ETPOSVoiceStream({
    csrfToken: "csrf",
    websocketFactory: () => socket,
    mediaDevices: {
      async getUserMedia() {
        mediaCalls += 1;
        throw new Error("must not be called");
      },
    },
    AudioContextClass: class {},
    AudioWorkletNodeClass: class {},
    location: { protocol: "https:", host: "example.test" },
  });

  await assert.rejects(
    controller.start(),
    (error) => error instanceof VoiceStreamError && error.code === "busy",
  );
  assert.equal(mediaCalls, 0);
  assert.equal(controller.state, "idle");
}

async function testStateMachineAndResidualFlush() {
  const stopped = { count: 0 };
  const socket = new FakeSocket("ready");
  const states = [];
  const partials = [];
  const finals = [];
  const errors = [];
  const audio = makeAudioEnvironment({
    onFlush(node) {
      const residual = new Int16Array([123]);
      node.port.onmessage?.({
        data: {
          type: "pcm",
          samples: residual.length,
          buffer: residual.buffer,
        },
      });
    },
  });

  const controller = new ETPOSVoiceStream({
    csrfToken: "csrf",
    websocketFactory: () => socket,
    mediaDevices: {
      async getUserMedia() {
        return fakeStream(stopped);
      },
    },
    AudioContextClass: audio.FakeAudioContext,
    AudioWorkletNodeClass: audio.FakeAudioWorkletNode,
    location: { protocol: "https:", host: "example.test" },
    onState: (state) => states.push(state),
    onPartial: (partial) => partials.push(partial),
    onFinal: (text) => finals.push(text),
    onError: (error) => errors.push(error),
  });

  await controller.start();
  assert.equal(controller.state, "recording");
  assert.deepEqual(states, ["starting", "recording"]);
  assert.equal(audio.created.context.sampleRate, 16000);
  assert.equal(audio.created.node.options.numberOfOutputs, 0);
  assert.equal(audio.created.source.connectedTo, audio.created.node);

  const first = new Int16Array([10, 20]);
  audio.created.node.port.onmessage({
    data: {
      type: "pcm",
      samples: first.length,
      buffer: first.buffer,
    },
  });

  socket.emit("message", {
    data: JSON.stringify({
      type: "partial",
      revision: 1,
      text: "aperçu",
      covered_samples: 2,
    }),
  });
  socket.emit("message", {
    data: JSON.stringify({
      type: "partial",
      revision: 1,
      text: "ancien",
      covered_samples: 2,
    }),
  });
  assert.deepEqual(partials, [{
    text: "aperçu",
    revision: 1,
    coveredSamples: 2,
  }]);

  await controller.stop();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(controller.state, "idle");
  assert.deepEqual(states, ["starting", "recording", "finishing", "idle"]);
  assert.deepEqual(finals, ["texte final"]);
  assert.deepEqual(errors, []);
  assert.equal(stopped.count, 1);

  const binaryFrames = socket.sent.filter((item) => item instanceof ArrayBuffer);
  assert.equal(binaryFrames.length, 2);

  const firstView = new DataView(binaryFrames[0]);
  assert.equal(firstView.getUint32(0, true), 0);
  assert.equal(Number(firstView.getBigUint64(4, true)), 0);

  const residualView = new DataView(binaryFrames[1]);
  assert.equal(residualView.getUint32(0, true), 1);
  assert.equal(Number(residualView.getBigUint64(4, true)), 2);

  const finish = socket.sent
    .filter((item) => typeof item === "string")
    .map((item) => JSON.parse(item))
    .find((item) => item.type === "finish");
  assert.deepEqual(finish, {
    type: "finish",
    last_sequence: 1,
    total_samples: 3,
  });
}

async function testLateFinalAfterCancelIsIgnored() {
  const stopped = { count: 0 };
  const socket = new FakeSocket("ready");
  const finals = [];
  const audio = makeAudioEnvironment();
  const controller = new ETPOSVoiceStream({
    csrfToken: "csrf",
    websocketFactory: () => socket,
    mediaDevices: {
      async getUserMedia() {
        return fakeStream(stopped);
      },
    },
    AudioContextClass: audio.FakeAudioContext,
    AudioWorkletNodeClass: audio.FakeAudioWorkletNode,
    location: { protocol: "https:", host: "example.test" },
    onFinal: (text) => finals.push(text),
  });

  await controller.start();
  const oldNonce = controller.sessionNonce;
  await controller.cancel();
  controller.handleServerMessage({ type: "final", text: "tardif" }, oldNonce);
  assert.deepEqual(finals, []);
  assert.equal(controller.state, "idle");
}

async function testEmptyStopFailsWithoutInvalidFinish() {
  const stopped = { count: 0 };
  const socket = new FakeSocket("ready");
  const errors = [];
  const audio = makeAudioEnvironment();
  const controller = new ETPOSVoiceStream({
    csrfToken: "csrf",
    websocketFactory: () => socket,
    mediaDevices: {
      async getUserMedia() {
        return fakeStream(stopped);
      },
    },
    AudioContextClass: audio.FakeAudioContext,
    AudioWorkletNodeClass: audio.FakeAudioWorkletNode,
    location: { protocol: "https:", host: "example.test" },
    onError: (error) => errors.push(error),
  });

  await controller.start();
  await controller.stop();
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(errors.length, 1);
  assert.equal(errors[0].code, "empty_audio");
  const finish = socket.sent
    .filter((item) => typeof item === "string")
    .map((item) => JSON.parse(item))
    .find((item) => item.type === "finish");
  assert.equal(finish, undefined);
}

async function testServerSampleLimitTriggersBoundedFinish() {
  const stopped = { count: 0 };
  const socket = new FakeSocket("ready");
  const finals = [];
  const audio = makeAudioEnvironment();
  const controller = new ETPOSVoiceStream({
    csrfToken: "csrf",
    websocketFactory: () => socket,
    mediaDevices: {
      async getUserMedia() {
        return fakeStream(stopped);
      },
    },
    AudioContextClass: audio.FakeAudioContext,
    AudioWorkletNodeClass: audio.FakeAudioWorkletNode,
    location: { protocol: "https:", host: "example.test" },
    onFinal: (text) => finals.push(text),
  });

  await controller.start();
  controller.limits.max_samples = 2;

  const chunk = new Int16Array([1, 2, 3]);
  audio.created.node.port.onmessage({
    data: {
      type: "pcm",
      samples: chunk.length,
      buffer: chunk.buffer,
    },
  });
  await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 0));

  const finish = socket.sent
    .filter((item) => typeof item === "string")
    .map((item) => JSON.parse(item))
    .find((item) => item.type === "finish");
  assert.deepEqual(finish, {
    type: "finish",
    last_sequence: 0,
    total_samples: 2,
  });
  assert.deepEqual(finals, ["texte final"]);
  assert.equal(controller.state, "idle");
}

async function testBackpressureFailsExplicitly() {
  const stopped = { count: 0 };
  const socket = new FakeSocket("ready");
  const errors = [];
  const audio = makeAudioEnvironment();

  const controller = new ETPOSVoiceStream({
    csrfToken: "csrf",
    websocketFactory: () => socket,
    mediaDevices: {
      async getUserMedia() {
        return fakeStream(stopped);
      },
    },
    AudioContextClass: audio.FakeAudioContext,
    AudioWorkletNodeClass: audio.FakeAudioWorkletNode,
    location: { protocol: "https:", host: "example.test" },
    onError: (error) => errors.push(error),
  });

  await controller.start();
  socket.bufferedAmount = 65536;
  const chunk = new Int16Array([1, 2, 3, 4]);
  audio.created.node.port.onmessage({
    data: {
      type: "pcm",
      samples: chunk.length,
      buffer: chunk.buffer,
    },
  });
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal(controller.state, "idle");
  assert.equal(errors.length, 1);
  assert.equal(errors[0].code, "backpressure");
  assert.equal(stopped.count, 1);
}

(async () => {
  await testFinalInsertionDecision();
  await testSupportDetection();
  await testDoubleStartIsRejected();
  await testMicrophoneRefusalReturnsToIdle();
  await testServerRefusalPreventsMicrophoneCapture();
  await testStateMachineAndResidualFlush();
  await testLateFinalAfterCancelIsIgnored();
  await testEmptyStopFailsWithoutInvalidFinish();
  await testServerSampleLimitTriggersBoundedFinish();
  await testBackpressureFailsExplicitly();
  process.stdout.write("voice-stream behavior tests passed\n");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

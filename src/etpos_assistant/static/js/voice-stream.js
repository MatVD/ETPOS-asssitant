(() => {
  const PCM_SAMPLE_RATE = 16000;
  const PCM_CHANNELS = 1;
  const PCM_ENCODING = "pcm_s16le";
  const CHUNK_HEADER_BYTES = 12;

  function finalInsertionDecision(snapshot, currentValue, text) {
    const transcript = String(text || "").trim();
    if (!transcript) return { mode: "none", text: "", selection: null };
    if (snapshot && String(currentValue ?? "") === String(snapshot.value ?? "")) {
      return {
        mode: "insert",
        text: transcript,
        selection: {
          start: Number(snapshot.start ?? 0),
          end: Number(snapshot.end ?? snapshot.start ?? 0),
        },
      };
    }
    return { mode: "review", text: transcript, selection: null };
  }

  class VoiceStreamError extends Error {
    constructor(message, code = "voice_stream_error") {
      super(message);
      this.name = "VoiceStreamError";
      this.code = code;
    }
  }

  class ETPOSVoiceStream {
    constructor(options = {}) {
      this.csrfToken = String(options.csrfToken || "");
      this.websocketPath = options.websocketPath || "/api/transcribe/stream";
      this.workletUrl = options.workletUrl || "/static/js/voice-worklet.js?v=2";
      this.onState = options.onState || (() => {});
      this.onPartial = options.onPartial || (() => {});
      this.onLevel = options.onLevel || (() => {});
      this.onFinal = options.onFinal || (() => {});
      this.onError = options.onError || (() => {});
      this.websocketFactory = options.websocketFactory || ((url) => new WebSocket(url));
      this.mediaDevices = options.mediaDevices || globalThis.navigator?.mediaDevices;
      this.AudioContextClass = options.AudioContextClass
        || globalThis.AudioContext
        || globalThis.webkitAudioContext;
      this.AudioWorkletNodeClass = options.AudioWorkletNodeClass
        || globalThis.AudioWorkletNode;
      this.location = options.location || globalThis.location;
      this.state = "idle";
      this.socket = null;
      this.stream = null;
      this.audioContext = null;
      this.source = null;
      this.worklet = null;
      this.limits = null;
      this.sequence = 0;
      this.totalSamples = 0;
      this.lastRevision = 0;
      this.flushCounter = 0;
      this.flushWaiters = new Map();
      this.sessionNonce = 0;
      this.closedByClient = false;
      this.finalTimer = null;
    }

    static supported(environment = globalThis) {
      const AudioContextClass = environment.AudioContext || environment.webkitAudioContext;
      return Boolean(
        environment.isSecureContext
        && environment.WebSocket
        && AudioContextClass
        && environment.AudioWorkletNode
        && environment.navigator?.mediaDevices?.getUserMedia,
      );
    }

    setState(next) {
      if (this.state === next) return;
      this.state = next;
      this.onState(next);
    }

    websocketUrl() {
      const location = this.location;
      if (!location) throw new VoiceStreamError("Origine du navigateur indisponible.", "location_missing");
      const protocol = location.protocol === "https:" ? "wss:" : "ws:";
      return `${protocol}//${location.host}${this.websocketPath}`;
    }

    async start() {
      if (this.state !== "idle") {
        throw new VoiceStreamError("Une dictée est déjà en cours.", "invalid_state");
      }

      this.setState("starting");
      this.closedByClient = false;
      this.sequence = 0;
      this.totalSamples = 0;
      this.lastRevision = 0;
      const nonce = ++this.sessionNonce;

      try {
        const ready = await this.openSocket(nonce);
        if (nonce !== this.sessionNonce) return;
        this.limits = ready.limits || {};
        await this.startAudio(nonce);
        if (nonce !== this.sessionNonce) return;
        this.setState("recording");
      } catch (error) {
        if (nonce === this.sessionNonce) {
          await this.cleanup();
          this.setState("idle");
          this.onError(this.normalizeError(error));
        }
        throw error;
      }
    }

    openSocket(nonce) {
      return new Promise((resolve, reject) => {
        let settled = false;
        const socket = this.websocketFactory(this.websocketUrl());
        this.socket = socket;

        const fail = (error) => {
          if (settled) return;
          settled = true;
          reject(error);
        };

        socket.addEventListener("open", () => {
          if (nonce !== this.sessionNonce) return;
          socket.send(JSON.stringify({
            type: "init",
            version: 1,
            csrf_token: this.csrfToken,
            format: {
              encoding: PCM_ENCODING,
              sample_rate: PCM_SAMPLE_RATE,
              channels: PCM_CHANNELS,
            },
          }));
        });

        socket.addEventListener("message", (event) => {
          if (nonce !== this.sessionNonce) return;
          let message;
          try {
            message = JSON.parse(String(event.data || ""));
          } catch {
            fail(new VoiceStreamError("Réponse de dictée invalide.", "invalid_server_message"));
            return;
          }

          if (!settled) {
            if (message.type === "ready") {
              settled = true;
              resolve(message);
              return;
            }
            if (message.type === "error") {
              fail(new VoiceStreamError(
                message.message || "La dictée en continu a été refusée.",
                message.code || "server_error",
              ));
            }
            return;
          }

          this.handleServerMessage(message, nonce);
        });

        socket.addEventListener("error", () => {
          fail(new VoiceStreamError("Connexion de dictée impossible.", "websocket_error"));
        });

        socket.addEventListener("close", () => {
          if (nonce !== this.sessionNonce || this.closedByClient) return;
          if (!settled) {
            fail(new VoiceStreamError("Connexion de dictée refusée.", "websocket_closed"));
            return;
          }
          if (this.state === "recording" || this.state === "finishing" || this.state === "starting") {
            void this.failTerminal(
              new VoiceStreamError("La connexion de dictée a été interrompue.", "websocket_closed"),
              nonce,
            );
          }
        });
      });
    }

    async startAudio(nonce) {
      if (!this.mediaDevices?.getUserMedia || !this.AudioContextClass || !this.AudioWorkletNodeClass) {
        throw new VoiceStreamError(
          "La dictée en continu n’est pas compatible avec ce navigateur.",
          "unsupported",
        );
      }

      const stream = await this.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      });
      if (nonce !== this.sessionNonce) {
        stream.getTracks?.().forEach((track) => track.stop());
        return;
      }
      this.stream = stream;

      const context = new this.AudioContextClass({ sampleRate: PCM_SAMPLE_RATE });
      this.audioContext = context;
      if (context.sampleRate !== PCM_SAMPLE_RATE) {
        throw new VoiceStreamError(
          "Le navigateur n’a pas fourni un flux audio à 16 kHz.",
          "sample_rate_mismatch",
        );
      }

      await context.audioWorklet.addModule(this.workletUrl);
      if (nonce !== this.sessionNonce) return;

      const nominalBytes = Number(this.limits?.nominal_chunk_bytes || 8000);
      const chunkSamples = Math.max(1, Math.floor(nominalBytes / 2));
      const source = context.createMediaStreamSource(stream);
      const worklet = new this.AudioWorkletNodeClass(
        context,
        "etpos-voice-capture",
        {
          numberOfInputs: 1,
          numberOfOutputs: 0,
          channelCount: 1,
          processorOptions: { chunkSamples },
        },
      );
      this.source = source;
      this.worklet = worklet;
      worklet.port.onmessage = (event) => this.handleWorkletMessage(event, nonce);
      source.connect(worklet);
      if (context.state === "suspended") await context.resume();
    }

    handleWorkletMessage(event, nonce) {
      if (nonce !== this.sessionNonce) return;
      const message = event?.data || {};
      if (message.type === "flushed") {
        const resolve = this.flushWaiters.get(message.requestId);
        if (resolve) {
          this.flushWaiters.delete(message.requestId);
          resolve();
        }
        return;
      }
      if (message.type === "level") {
        if (this.state === "recording") {
          const value = Number(message.value || 0);
          this.onLevel(Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0)));
        }
        return;
      }
      if (
        message.type !== "pcm"
        || !["recording", "finishing"].includes(this.state)
      ) return;

      const buffer = message.buffer;
      const samples = Number(message.samples || 0);
      if (!(buffer instanceof ArrayBuffer) || samples <= 0 || buffer.byteLength !== samples * 2) {
        void this.failTerminal(
          new VoiceStreamError("Bloc PCM invalide produit par le navigateur.", "invalid_pcm"),
          nonce,
        );
        return;
      }

      try {
        this.sendPcm(buffer, samples);
      } catch (error) {
        void this.failTerminal(error, nonce);
      }
    }

    sendPcm(pcmBuffer, samples) {
      const socket = this.socket;
      if (!socket || socket.readyState !== 1) {
        throw new VoiceStreamError("Connexion de dictée indisponible.", "socket_not_open");
      }

      const frameBytes = CHUNK_HEADER_BYTES + pcmBuffer.byteLength;
      const maxMessage = Number(this.limits?.max_message_bytes || 0);
      if (maxMessage > 0 && frameBytes > maxMessage) {
        throw new VoiceStreamError("Bloc audio trop volumineux.", "message_too_large");
      }

      const maxQueue = Number(this.limits?.max_queue_messages || 4);
      const maxBuffered = Math.max(frameBytes, maxMessage || frameBytes) * Math.max(1, maxQueue);
      if (socket.bufferedAmount + frameBytes > maxBuffered) {
        throw new VoiceStreamError(
          "La connexion est trop lente pour transmettre la dictée sans perte.",
          "backpressure",
        );
      }

      const maxSamples = Number(this.limits?.max_samples || 0);
      let acceptedSamples = samples;
      let acceptedBuffer = pcmBuffer;
      if (maxSamples > 0) {
        const remaining = maxSamples - this.totalSamples;
        if (remaining <= 0) {
          if (this.state === "recording") void this.stop();
          return;
        }
        if (acceptedSamples > remaining) {
          acceptedSamples = remaining;
          acceptedBuffer = pcmBuffer.slice(0, acceptedSamples * 2);
        }
      }

      const acceptedFrameBytes = CHUNK_HEADER_BYTES + acceptedBuffer.byteLength;
      const frame = new ArrayBuffer(acceptedFrameBytes);
      const view = new DataView(frame);
      view.setUint32(0, this.sequence, true);
      view.setBigUint64(4, BigInt(this.totalSamples), true);
      new Uint8Array(frame, CHUNK_HEADER_BYTES).set(new Uint8Array(acceptedBuffer));
      socket.send(frame);
      this.sequence += 1;
      this.totalSamples += acceptedSamples;
      if (maxSamples > 0 && this.totalSamples >= maxSamples && this.state === "recording") {
        queueMicrotask(() => {
          if (this.state === "recording") void this.stop();
        });
      }
    }

    handleServerMessage(message, nonce) {
      if (nonce !== this.sessionNonce) return;

      if (message.type === "partial") {
        const revision = Number(message.revision || 0);
        if (revision <= this.lastRevision || this.state !== "recording") return;
        this.lastRevision = revision;
        this.onPartial({
          text: String(message.text || ""),
          revision,
          coveredSamples: Number(message.covered_samples || 0),
        });
        return;
      }

      if (message.type === "final") {
        if (this.state !== "finishing") return;
        const text = String(message.text || "");
        void this.completeFinal(text, nonce);
        return;
      }

      if (message.type === "error") {
        void this.failTerminal(
          new VoiceStreamError(
            message.message || "La dictée n’a pas abouti.",
            message.code || "server_error",
          ),
          nonce,
        );
      }
    }

    async stop() {
      if (this.state !== "recording") return;
      this.setState("finishing");
      const nonce = this.sessionNonce;

      try {
        await this.flushWorklet(nonce);
        if (nonce !== this.sessionNonce) return;
        if (this.sequence === 0 || this.totalSamples === 0) {
          throw new VoiceStreamError("Aucun son n’a été enregistré.", "empty_audio");
        }
        const socket = this.socket;
        if (!socket || socket.readyState !== 1) {
          throw new VoiceStreamError("Connexion de dictée indisponible.", "socket_not_open");
        }
        socket.send(JSON.stringify({
          type: "finish",
          last_sequence: this.sequence - 1,
          total_samples: this.totalSamples,
        }));
        const finalizationSeconds = Number(this.limits?.finalization_timeout_seconds || 0);
        if (finalizationSeconds > 0) {
          this.finalTimer = setTimeout(() => {
            void this.failTerminal(
              new VoiceStreamError(
                "La finalisation de la dictée a dépassé le délai autorisé.",
                "finalization_timeout",
              ),
              nonce,
            );
          }, (finalizationSeconds * 1000) + 1000);
        }
      } catch (error) {
        await this.failTerminal(error, nonce);
      }
    }

    flushWorklet(nonce) {
      if (!this.worklet) return Promise.resolve();
      const requestId = ++this.flushCounter;
      return new Promise((resolve, reject) => {
        const timeout = setTimeout(() => {
          this.flushWaiters.delete(requestId);
          reject(new VoiceStreamError("Le dernier bloc audio n’a pas pu être vidé.", "flush_timeout"));
        }, 2000);

        this.flushWaiters.set(requestId, () => {
          clearTimeout(timeout);
          if (nonce === this.sessionNonce) resolve();
          else reject(new VoiceStreamError("Dictée annulée.", "cancelled"));
        });
        this.worklet.port.postMessage({ type: "flush", requestId });
      });
    }

    async completeFinal(text, nonce) {
      if (nonce !== this.sessionNonce) return;
      await this.cleanup();
      this.setState("idle");
      this.onFinal(text);
    }

    async failTerminal(error, nonce = this.sessionNonce) {
      if (nonce !== this.sessionNonce) return;
      const normalized = this.normalizeError(error);
      await this.cleanup();
      this.setState("idle");
      this.onError(normalized);
    }

    async cancel() {
      this.sessionNonce += 1;
      await this.cleanup();
      this.setState("idle");
    }

    normalizeError(error) {
      if (error instanceof VoiceStreamError) return error;
      let message = error?.message || "La dictée n’a pas abouti.";
      if (error?.name === "NotAllowedError" || error?.name === "SecurityError") {
        message = "Accès au microphone refusé. Autorisez-le dans le navigateur puis réessayez.";
      } else if (error?.name === "NotFoundError") {
        message = "Aucun microphone n’a été détecté.";
      }
      return new VoiceStreamError(message, error?.name || "voice_stream_error");
    }

    async cleanup() {
      this.closedByClient = true;
      this.flushWaiters.clear();
      if (this.finalTimer) {
        clearTimeout(this.finalTimer);
        this.finalTimer = null;
      }
      try {
        this.source?.disconnect?.();
      } catch {}
      try {
        this.worklet?.disconnect?.();
      } catch {}
      this.stream?.getTracks?.().forEach((track) => track.stop());
      if (this.audioContext && this.audioContext.state !== "closed") {
        try {
          await this.audioContext.close();
        } catch {}
      }
      if (this.socket && this.socket.readyState < 2) {
        try {
          this.socket.close(1000, "client cleanup");
        } catch {}
      }
      this.socket = null;
      this.stream = null;
      this.audioContext = null;
      this.source = null;
      this.worklet = null;
      this.limits = null;
    }
  }

  const api = { ETPOSVoiceStream, VoiceStreamError, finalInsertionDecision };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window !== "undefined") Object.assign(window, api);
})();

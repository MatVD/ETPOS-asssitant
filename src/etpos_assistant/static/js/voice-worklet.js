class ETPOSVoiceCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const requested = Number(options?.processorOptions?.chunkSamples || 4000);
    this.chunkSamples = Number.isInteger(requested) && requested > 0 ? requested : 4000;
    this.buffer = new Int16Array(this.chunkSamples);
    this.offset = 0;
    this.accepting = true;

    this.port.onmessage = (event) => {
      const message = event?.data || {};
      if (message.type !== "flush") return;
      this.accepting = false;
      this.flush();
      this.port.postMessage({
        type: "flushed",
        requestId: message.requestId,
      });
    };
  }

  encodeSample(value) {
    const sample = Math.max(-1, Math.min(1, Number(value) || 0));
    return sample < 0
      ? Math.round(sample * 0x8000)
      : Math.round(sample * 0x7fff);
  }

  emit(length) {
    if (length <= 0) return;
    const chunk = this.buffer.slice(0, length);
    this.port.postMessage(
      {
        type: "pcm",
        samples: length,
        buffer: chunk.buffer,
      },
      [chunk.buffer],
    );
  }

  flush() {
    if (this.offset > 0) {
      this.emit(this.offset);
      this.offset = 0;
    }
  }

  process(inputs) {
    if (!this.accepting) return true;
    const input = inputs?.[0]?.[0];
    if (!input) return true;

    for (let index = 0; index < input.length; index += 1) {
      this.buffer[this.offset] = this.encodeSample(input[index]);
      this.offset += 1;
      if (this.offset === this.buffer.length) {
        this.emit(this.offset);
        this.offset = 0;
      }
    }
    return true;
  }
}

registerProcessor("etpos-voice-capture", ETPOSVoiceCaptureProcessor);

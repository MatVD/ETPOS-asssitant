class ETPOSVoiceCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const requested = Number(options?.processorOptions?.chunkSamples || 4000);
    this.chunkSamples = Number.isInteger(requested) && requested > 0 ? requested : 4000;
    this.buffer = new Int16Array(this.chunkSamples);
    this.offset = 0;
    this.accepting = true;
    this.levelSumSquares = 0;
    this.levelSampleCount = 0;
    this.levelWindowSamples = 800;

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

  emitLevel() {
    if (this.levelSampleCount <= 0) return;
    const rms = Math.sqrt(this.levelSumSquares / this.levelSampleCount);
    this.port.postMessage({
      type: "level",
      value: Math.max(0, Math.min(1, rms)),
    });
    this.levelSumSquares = 0;
    this.levelSampleCount = 0;
  }

  process(inputs) {
    if (!this.accepting) return true;
    const input = inputs?.[0]?.[0];
    if (!input) return true;

    for (let index = 0; index < input.length; index += 1) {
      const sample = Math.max(-1, Math.min(1, Number(input[index]) || 0));
      this.levelSumSquares += sample * sample;
      this.levelSampleCount += 1;
      this.buffer[this.offset] = this.encodeSample(sample);
      this.offset += 1;
      if (this.offset === this.buffer.length) {
        this.emit(this.offset);
        this.offset = 0;
      }
    }
    if (this.levelSampleCount >= this.levelWindowSamples) this.emitLevel();
    return true;
  }
}

registerProcessor("etpos-voice-capture", ETPOSVoiceCaptureProcessor);

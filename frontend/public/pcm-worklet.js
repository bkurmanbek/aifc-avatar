class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const config = options.processorOptions || {};
    this.targetSampleRate = Number(config.targetSampleRate) || 16000;
    this.frameSamples = Math.max(160, Number(config.frameSamples) || 480);
    this.step = sampleRate / this.targetSampleRate;
    this.sourceBuffer = new Float32Array(0);
    this.sourcePos = 0;
    this.frame = new Float32Array(this.frameSamples);
    this.frameIndex = 0;
  }

  process(inputs, outputs) {
    const output = outputs[0];
    if (output) {
      for (const channel of output) channel.fill(0);
    }

    const input = inputs[0]?.[0];
    if (!input || input.length === 0) return true;

    const combined = new Float32Array(this.sourceBuffer.length + input.length);
    combined.set(this.sourceBuffer);
    combined.set(input, this.sourceBuffer.length);

    while (this.sourcePos + 1 < combined.length) {
      const leftIndex = Math.floor(this.sourcePos);
      const rightIndex = leftIndex + 1;
      const fraction = this.sourcePos - leftIndex;
      const sample = combined[leftIndex] + (combined[rightIndex] - combined[leftIndex]) * fraction;
      this.frame[this.frameIndex] = Math.max(-1, Math.min(1, sample));
      this.frameIndex += 1;
      if (this.frameIndex >= this.frameSamples) {
        const frame = this.frame.slice();
        this.port.postMessage(frame.buffer, [frame.buffer]);
        this.frameIndex = 0;
      }
      this.sourcePos += this.step;
    }

    const consumed = Math.max(0, Math.floor(this.sourcePos));
    this.sourceBuffer = combined.slice(consumed);
    this.sourcePos -= consumed;

    return true;
  }
}

registerProcessor("pcm-capture", PcmCaptureProcessor);

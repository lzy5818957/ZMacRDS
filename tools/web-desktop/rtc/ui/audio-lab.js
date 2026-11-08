class Calibration extends AudioWorkletProcessor {
  constructor() {
    super();
    this.size = 4096;
    this.buf = [new Float32Array(this.size), new Float32Array(this.size)];
    this.window = Float32Array.from({length:this.size}, (_,i)=>.5-.5*Math.cos(2*Math.PI*i/(this.size-1)));
    this.offset = 0;
    this.sounding = false;
    this.lastOnset = -1;
  }
  power(frequency) {
    // Calculate each channel independently; opposite-phase leakage must
    // still be visible in the final measurement.
    const omega = 2*Math.PI*frequency/sampleRate;
    const factor = 2*Math.cos(omega);
    let power = 0;
    for (const channel of this.buf) {
      let a = 0, b = 0;
      for (let i=0; i<channel.length; i++) {
        const c = this.window[i]*channel[i]+factor*a-b;
        b = a; a = c;
      }
      power += Math.max(0, a*a+b*b-factor*a*b)/(this.size*this.size/4);
    }
    return 10*Math.log10(Math.max(1e-12,power/2));
  }
  process(inputs) {
    const [left,right=left] = inputs[0];
    if (!left) return true;
    let peak = 0;
    let onset;
    for (let i=0; i<left.length; i++) {
      const magnitude = Math.max(Math.abs(left[i]),Math.abs(right[i]));
      if (magnitude > .035 && onset === undefined) onset = (currentFrame+i)/sampleRate;
      peak = Math.max(peak,magnitude);
      this.buf[0][this.offset] = left[i];
      this.buf[1][this.offset] = right[i];
      if (++this.offset === this.size) {
        this.offset = 0;
        this.port.postMessage({powers:[this.power(440),this.power(880)]});
      }
    }
    if (!this.sounding && onset !== undefined && onset-this.lastOnset>.25) {
      this.lastOnset = onset;
      this.port.postMessage({onset});
    }
    this.sounding = peak >= .004;
    return true;
  }
}
registerProcessor('calibration', Calibration);

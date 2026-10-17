/** Browser audio needs a trusted gesture on first use; do not leave startup pending indefinitely. */
export class AudioActivationError extends Error {
  constructor() {
    super('浏览器尚未解锁声音，请点击“进入语音对话”启用麦克风和声音。');
    this.name = 'AudioActivationRequired';
  }
}

export function resumeAudioContext(context: AudioContext, timeoutMs = 1500): Promise<void> {
  // Call resume synchronously so the original click/Enter still authorizes audio.
  const resumed = context.resume();
  if (context.state === 'running') return resumed;
  return new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => reject(new AudioActivationError()), timeoutMs);
    resumed.then(() => {
      clearTimeout(timer);
      if (context.state === 'running') resolve();
      else reject(new AudioActivationError());
    }, error => { clearTimeout(timer); reject(error); });
  });
}

// The acceptance budget is query submission to the first received PCM packet
// containing a real nonzero sample. Render timings remain separate diagnostics.
export const QUERY_TO_NONZERO_PCM_TARGET_MS = 1500;
export const QUERY_TO_NONZERO_PCM_TARGET_LABEL = `${QUERY_TO_NONZERO_PCM_TARGET_MS / 1000} 秒`;

export function pcmTargetClass(elapsedMs?: number | null): string {
  if (elapsedMs == null || !Number.isFinite(elapsedMs) || elapsedMs < 0) return '';
  return elapsedMs <= QUERY_TO_NONZERO_PCM_TARGET_MS ? 'performance-within-target' : 'performance-over-target';
}

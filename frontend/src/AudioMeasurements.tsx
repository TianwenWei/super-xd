import { useState } from 'react';
import type { AudioMeasurement, RecognitionMeasurement } from './types';
import { QUERY_TO_NONZERO_PCM_TARGET_LABEL, QUERY_TO_NONZERO_PCM_TARGET_MS } from './performanceTarget';

export default function AudioMeasurements({ records, recognitionRecords = [], sessionId, pageId }: { records: AudioMeasurement[]; recognitionRecords?: RecognitionMeasurement[]; sessionId: string; pageId: string }) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState('');
  async function copy() {
    try { await navigator.clipboard.writeText(JSON.stringify({ schema: 'super-xd-audio-measurements-v1', session_id: sessionId, page_id: pageId, browser_user_agent: navigator.userAgent, exported_at: new Date().toISOString(), performance_time_origin: performance.timeOrigin, time_domain: 'same-page performance.now milliseconds', render_semantics: 'AudioWorklet first real PCM sample consumed; main-thread notification observation includes message delay, not acoustic speaker onset', records, recognition_records: recognitionRecords }, null, 2)); setCopied(true); setError(''); }
    catch { setError('复制失败，可选择下方 JSON 复制。'); }
  }
  return <details className="audio-measurements"><summary>音频测量 <span className="mono">{records.length + recognitionRecords.length} 条</span></summary><div className="audio-measurement-body"><p>主指标：提交 → 首个含非零样本的 PCM 包，目标 ≤ {QUERY_TO_NONZERO_PCM_TARGET_LABEL}（{QUERY_TO_NONZERO_PCM_TARGET_MS} ms）。合法首包（含静音）另计。入队不等于播放；渲染通知含线程传递延迟，不代表扬声器声学出声。PCM 24kHz / mono / s16le。</p><button onClick={copy} type="button">复制测量 JSON</button>{copied && <span role="status">已复制本次测量</span>}{error && <span role="status">{error}</span>}<pre tabIndex={0}>{JSON.stringify({ records, recognition_records: recognitionRecords }, null, 2)}</pre></div></details>;
}

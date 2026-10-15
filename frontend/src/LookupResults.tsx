import './lookup-results.css';

type Place = { place_id: string; name: string; address: string; maps_url?: string;
  attributions?: Array<{ provider?: string; providerUri?: string }> };
export type LookupRecord = { call_id: string; tool: string; result: {
  ok: boolean; kind?: string; provider?: string; places?: Place[]; place?: Place;
  date?: string; timezone?: string; notice?: string; reason?: string; queried_at?: string;
  summary?: { temperature_c?: number; min_c?: number; max_c?: number; condition?: string;
    day_condition?: string; night_condition?: string; rain_probability_percent?: number };
} };

function safeLink(value?: string): string | undefined {
  try { const url = new URL(value || ''); return url.protocol === 'https:' ? url.href : undefined; }
  catch { return undefined; }
}

export default function LookupResults({ records }: { records: LookupRecord[] }) {
  return <div className="lookup-results">{records.map(({call_id, result: r}) => <section className="lookup-card" key={call_id}>
    <div className="lookup-source"><strong>{r.provider || '实时查询'}</strong><span>{r.date || ''}</span></div>
    {!r.ok ? <p>{r.reason || '未取得查询结果。'}</p> : r.kind === 'places' ? <>
      <ol>{(r.places || []).map(place => <li key={place.place_id}>
        {safeLink(place.maps_url) ? <a href={safeLink(place.maps_url)} target="_blank" rel="noreferrer">{place.name} ↗</a> : <strong>{place.name}</strong>}
        <p>{place.address}</p>
        {(place.attributions || []).map((a, i) => <small key={i}>{safeLink(a.providerUri) ? <a href={safeLink(a.providerUri)} target="_blank" rel="noreferrer">{a.provider}</a> : a.provider}</small>)}
      </li>)}</ol><small>{r.notice} 可告诉助手选择哪个地点进行模拟导航。</small>
    </> : <>
      <strong>{r.place?.name} · {r.summary?.condition || r.summary?.day_condition || '天气预报'}</strong>
      <p>{r.summary?.temperature_c != null ? `${r.summary.temperature_c}°C` : `${r.summary?.min_c ?? '—'}–${r.summary?.max_c ?? '—'}°C`}
        {r.summary?.rain_probability_percent != null && ` · 降水概率 ${r.summary.rain_probability_percent}%`}</p>
      <small>{r.notice}</small>
    </>}
    {r.queried_at && <time dateTime={r.queried_at}>{new Date(r.queried_at).toLocaleString()}</time>}
  </section>)}</div>;
}

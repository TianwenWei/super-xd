import './lookup-results.css';

import ManualResults from './ManualResults';
import WeatherCard from './WeatherCard';
import { safeLink } from './lookupViews';
import type { LookupRecord } from './lookupViews';
export type { LookupRecord } from './lookupViews';

export default function LookupResults({ records, send, connected = false }: { records: LookupRecord[]; send?: (value: Record<string, unknown>) => void; connected?: boolean }) {
  return <div className="lookup-results">{records.map(({call_id, tool, result: r}) => r.kind === 'weather' || tool === 'get_weather'
    ? <section className="lookup-card weather-card" aria-label="天气卡片" key={call_id}><WeatherCard result={r}/></section>
    : <section className="lookup-card" key={call_id}>
    <div className="lookup-source"><strong>{r.provider || '实时查询'}</strong><span>{r.date || ''}</span></div>
    {!r.ok ? <p>{r.reason || '未取得查询结果。'}</p> : r.kind === 'places' ? <>
      <ol>{(r.places || []).map(place => <li key={place.place_id}>
        {safeLink(place.maps_url) ? <a href={safeLink(place.maps_url)} target="_blank" rel="noreferrer">{place.name} ↗</a> : <strong>{place.name}</strong>}
        <p>{place.address}</p>{send && <div className="place-cockpit-actions"><button disabled={!connected} onClick={() => send({ type: 'cockpit.control', domain: 'navigation', action: 'set_origin', arguments: { place_ref: place.place_ref || place.place_id } })}>设为出发地</button><button disabled={!connected} onClick={() => send({ type: 'cockpit.control', domain: 'navigation', action: 'start', arguments: { place_ref: place.place_ref || place.place_id } })}>启动模拟导航</button><button disabled={!connected} onClick={() => send({ type: 'cockpit.control', domain: 'navigation', action: 'add_waypoint', arguments: { place_ref: place.place_ref || place.place_id } })}>添加途经点</button></div>}
        {(place.attributions || []).map((a, i) => <small key={i}>{safeLink(a.providerUri) ? <a href={safeLink(a.providerUri)} target="_blank" rel="noreferrer">{a.provider}</a> : a.provider}</small>)}
      </li>)}</ol><small>{r.notice} 可选择地点设为出发地、启动模拟导航或添加途经点。</small>
    </> : r.kind === 'media' ? <><ol>{(r.items || []).map(item => <li key={item.content_id}><strong>{item.title}</strong>{item.artist && <p>{item.artist}</p>}{send && <div className="place-cockpit-actions"><button disabled={!connected} onClick={() => send({ type: 'cockpit.control', domain: 'media', action: 'play', arguments: { content_id: item.content_id } })}>播放</button></div>}</li>)}</ol><small>播放成功以浏览器播放器的实际回执为准。</small></> : r.kind === 'web_search' ? <>
      <strong>{r.query}</strong>
      {(r.results || []).length ? <ol>{r.results!.map((source, index) => <li key={`${source.url || ''}:${index}`}>
        {safeLink(source.url) ? <a href={safeLink(source.url)} target="_blank" rel="noreferrer">{source.title || source.url} ↗</a> : <strong>{source.title || '网页结果'}</strong>}
        {source.content && <details><summary>查看搜索摘要</summary><p>{source.content}</p></details>}
      </li>)}</ol> : <p>没有找到可用网页结果。</p>}
      <small>{r.notice}</small>
    </> : r.kind === 'manual' ? <ManualResults result={r}/> : <p>{r.notice || '暂无可展示的结构化结果。'}</p>}
    {r.queried_at && <time dateTime={r.queried_at}>{new Date(r.queried_at).toLocaleString()}</time>}
  </section>)}</div>;
}

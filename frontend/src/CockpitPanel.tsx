import { useEffect, useRef, useState } from 'react';
import type { DeviceQueryRegistry } from './DeviceQueries';
import type { MediaPlayback } from './MediaPlayback';
import type { MediaCatalog, MediaKind, MediaState, NavigationState } from './cockpitTypes';
import './cockpit.css';

export function playbackTime(seconds?: number): string {
  if (seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return '—';
  const whole = Math.floor(seconds); return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
}

export default function CockpitPanel({ engine, device, choices, media, navigation, connected, sessionId, send }: {
  engine: MediaPlayback; device: DeviceQueryRegistry; choices: MediaCatalog | null; media: MediaState; navigation: NavigationState; connected: boolean; sessionId: string;
  send: (value: Record<string, unknown>) => void;
}) {
  const audio = useRef<HTMLAudioElement>(null), video = useRef<HTMLVideoElement>(null);
  const [catalog, setCatalog] = useState<MediaCatalog>({ items: [] });
  const [catalogError, setCatalogError] = useState('');
  const [kind, setKind] = useState<MediaKind>('music');
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState('');
  useEffect(() => { if (choices) { setCatalog(current => ({ ...current, ...choices, providers: choices.providers || current.providers })); setCatalogError(''); } }, [choices]);
  const [volume, setVolume] = useState<number | null>(null);
  const [position, setPosition] = useState<number | null>(null);
  useEffect(() => { if (audio.current && video.current) engine.attach(audio.current, video.current); }, [engine]);
  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    void fetch(`/api/media/catalog?kind=${encodeURIComponent(kind)}`).then(async response => {
      const data = await response.json();
      if (!response.ok) throw new Error(data.error?.message || data.detail || `媒体目录读取失败（${response.status}）`);
      if (!Array.isArray(data.items)) throw new Error('媒体服务没有返回有效目录。');
      if (!cancelled) { device.setMediaCatalog(data.items); setCatalog(data); setCatalogError(''); }
    }).catch(error => { if (!cancelled) setCatalogError(error instanceof Error ? error.message : String(error)); })
      .finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [refresh, sessionId, kind]);
  const control = (domain: 'media' | 'navigation', action: string, args: Record<string, unknown> = {}) => send({ type: 'cockpit.control', domain, action, arguments: args });
  const status = media.buffering ? '缓冲中' : { playing: '实际播放中', paused: '已暂停', stopped: '已停止', closed: '未播放' }[media.status];
  const provider = catalog.providers?.[kind] || catalog;
  const binding = catalog.providers?.[kind]?.binding;
  const items = catalog.items.filter(item => item.kind === kind);
  const duration = media.duration_seconds;
  const route = navigation.route;
  const distance = typeof route?.distance_meters === 'number' ? `${(route.distance_meters / 1000).toFixed(1)} km` : undefined;
  const etaSeconds = typeof route?.duration_seconds === 'number' ? route.duration_seconds : undefined;
  return <section className="cockpit-panel" aria-label="媒体与导航">
    <details open className="cockpit-module">
      <summary><strong>媒体播放</strong><span>{status}</span></summary>
      <audio ref={audio} preload="metadata" aria-label="座舱音频播放器"/>
      <video ref={video} preload="metadata" playsInline hidden={media.kind !== 'video' || media.status === 'closed'} aria-label="座舱视频播放器"/>
      <p className="cockpit-provider">{binding ? `${binding} · ` : ''}{provider.provider === 'device_catalog' ? '本机媒体目录' : provider.provider === 'local_catalog' ? '本地媒体目录（开发验证）' : provider.provider === 'tencent_gateway' && kind === 'music' ? '腾讯音乐网关' : provider.provider || '媒体服务'} · {provider.configured === false || provider.status === 'not_configured' ? '未配置' : busy ? '读取目录中' : catalog.items.length ? '目录已连接' : '暂无内容'}{provider.execution_location === 'device' ? ' · 端侧查询' : provider.execution_location === 'cloud' ? ' · 云端查询' : ''}<button type="button" disabled={busy} onClick={() => setRefresh(value => value + 1)}>刷新</button></p>
      {(catalogError || provider.reason || catalog.error?.message) && <p role="status" className="cockpit-notice">{catalogError || provider.reason || catalog.error?.message}</p>}
      <>{provider.provider === 'device_catalog' && kind !== 'radio' && <label className="device-media-import">导入本机音视频<input type="file" multiple accept="audio/*,video/*" onChange={event => { const items = device.importMediaFiles([...event.target.files || []]); setCatalog(current => ({ ...current, items: [...current.items, ...items], configured: true })); }}/></label>}</><div className="cockpit-kind" aria-label="媒体类型">{(['music', 'video', 'radio'] as MediaKind[]).map(item => <button type="button" key={item} aria-pressed={kind === item} onClick={() => setKind(item)}>{{ music: '音乐', video: '视频', radio: '网络电台' }[item]}</button>)}</div>
      <form className="media-search" onSubmit={event => { event.preventDefault(); if (query.trim()) control('media', 'search', { kind, query: query.trim() }); }}><input aria-label="搜索媒体内容" value={query} onChange={event => setQuery(event.target.value)} placeholder="歌名、视频或电台名称"/><button disabled={!connected || !query.trim()} type="submit">搜索</button></form><div className="media-candidates">{items.map(item => <button type="button" key={item.content_id} disabled={!connected} className={media.content_id === item.content_id ? 'selected' : ''} onClick={() => provider.provider === 'device_catalog' && !item.content_id.startsWith('media:') ? control('media', 'search', { kind: item.kind, query: item.title }) : control('media', 'play', { content_id: item.content_id })}><strong>{item.title}</strong><small>{provider.provider === 'device_catalog' && !item.content_id.startsWith('media:') ? '点击查询并选择播放内容' : item.artist || item.provider || '点击请求播放'}</small></button>)}{!items.length && <p className="muted">{kind === 'radio' ? '尚未配置可播放的网络电台流。FM 调谐属于模拟硬件，不会在这里产生音频。' : '没有配置此类型的可播放内容。'}</p>}</div>
      {media.status !== 'closed' && <div className="media-now"><strong>{media.title || media.content_id || '当前媒体'}</strong><span aria-live="polite">{status}{media.focus_reason ? media.focus_source?.includes('capture') ? ' · 说话时暂停' : media.kind === 'video' ? ' · 语音期间暂停' : ' · 语音期间降低音量' : ''}</span><div className="media-progress"><time>{playbackTime(media.position_seconds)}</time>{duration !== undefined ? <><input type="range" aria-label="媒体播放进度" min={0} max={duration} step={1} value={position ?? media.position_seconds} disabled={!connected} onChange={e => setPosition(Number(e.target.value))} onPointerUp={e => { control('media', 'seek', { position_seconds: Number(e.currentTarget.value) }); setPosition(null); }} onKeyUp={e => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) { control('media', 'seek', { position_seconds: Number(e.currentTarget.value) }); setPosition(null); } }}/><time>{playbackTime(duration)}</time></> : <small>直播 / 时长未提供</small>}</div><div className="cockpit-controls"><button disabled={!connected} onClick={() => control('media', 'previous')}>上一首</button><button disabled={!connected} onClick={() => control('media', media.desired_status === 'playing' ? 'pause' : 'resume')}>{media.desired_status === 'playing' ? '暂停' : '继续播放'}</button><button disabled={!connected} onClick={() => control('media', 'next')}>下一首</button><button disabled={!connected} onClick={() => control('media', 'stop')}>停止</button><button disabled={!connected} onClick={() => control('media', 'exit')}>退出</button></div><label className="media-volume">音量<input type="range" aria-label="媒体音量" min={0} max={100} step={5} value={volume ?? media.volume} disabled={!connected} onChange={e => setVolume(Number(e.target.value))} onPointerUp={e => { control('media', 'volume', { volume: Number(e.currentTarget.value) }); setVolume(null); }} onKeyUp={e => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) { control('media', 'volume', { volume: Number(e.currentTarget.value) }); setVolume(null); } }}/><span>{Math.round(volume ?? media.volume)}%</span></label>{media.error && <p role="status" className="cockpit-notice">{media.error}</p>}</div>}
    </details>
    <details open className="cockpit-module navigation-module"><summary><strong>模拟导航</strong><span>{navigation.active ? '进行中' : '未启动'}</span></summary>{navigation.origin && <p className="navigation-origin">出发地：<strong>{navigation.origin.name}</strong>{navigation.origin.address && <small>{navigation.origin.address}</small>}</p>}{navigation.destination ? <><h3>{navigation.destination.name}</h3><p>{navigation.destination.address}</p></> : <p className="muted">搜索地点后，可从候选卡设定出发地、启动导航或添加途经点。</p>}{navigation.waypoints.length > 0 && <ol className="navigation-waypoints">{navigation.waypoints.map((place, index) => <li key={`${place.place_id || place.name}:${index}`}><span>{place.name}</span><button disabled={!connected} onClick={() => control('navigation', 'remove_waypoint', { waypoint_ref: place.place_ref || place.place_id })} aria-label={`移除途经点${place.name}`}>移除</button></li>)}</ol>}{route ? <p className="cockpit-route">{route.provider || navigation.provider || '路线服务'}{distance ? ` · ${distance}` : ''}{etaSeconds !== undefined ? ` · 预计 ${Math.ceil(etaSeconds / 60)} 分钟` : ' · 未提供预计用时'}</p> : <p className="cockpit-notice">{navigation.reason || '路线服务未提供路线；没有可核实的距离或预计到达时间。'}</p>}{navigation.active && <button disabled={!connected} onClick={() => control('navigation', 'route')}>查询路线</button>}{navigation.active && <button disabled={!connected} onClick={() => control('navigation', 'cancel')}>取消导航</button>}<small>目的地与途经点为座舱模拟状态，未操控真实车辆。</small></details>
  </section>;
}

import { useState } from 'react';
import type { LookupResult } from './lookupViews';
import { safeLink, weatherIconUrl, weatherTemperature } from './lookupViews';

function WeatherSymbol({ code, url }: { code?: number; url?: string }) {
  const [failed, setFailed] = useState(false);
  const snow = [71, 73, 75, 77, 85, 86].includes(code ?? -1);
  const rain = [51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82].includes(code ?? -1);
  const storm = [95, 96, 99].includes(code ?? -1);
  const clear = code === 0;
  const fog = code === 45 || code === 48;
  if (url && !failed) return <img className="weather-symbol" src={url} alt="" onError={() => setFailed(true)}/>;
  return <svg className="weather-symbol" width="52" height="52" viewBox="0 0 48 48" fill="none" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {(clear || code === 1 || code === 2) && <g stroke="#c79540" fill="#f5d587"><circle cx={clear ? 24 : 20} cy={clear ? 24 : 19} r="8"/><path d={clear ? 'M24 7v3M24 38v3M7 24h3M38 24h3M12 12l2 2M34 34l2 2M12 36l2-2M34 14l2-2' : 'M20 5v2M6 19h2M10 9l2 2M29 9l2-2'}/></g>}
    {!clear && <path d="M12 34a7 7 0 0 1 0-14 10 10 0 0 1 18-3 8.5 8.5 0 1 1 6 17H12Z" fill="#fff" stroke="#79a3b2"/>}
    {rain && <path d="m16 38-2 4m10-4-2 4m10-4-2 4" stroke="#5b99bb"/>}
    {snow && <g stroke="#5b99bb"><path d="M16 38v6m-3-3h6M30 38v6m-3-3h6"/></g>}
    {storm && <path d="m25 31-5 8h5l-3 6 9-10h-6l3-4" fill="#e8b552" stroke="#c79540"/>}
    {fog && <path d="M10 39h28M15 44h20" stroke="#8aa6af"/>}
  </svg>;
}

export default function WeatherCard({ result: r }: { result: LookupResult }) {
  const summary = r.summary;
  const rain = summary?.rain_probability_percent;
  const temperature = weatherTemperature(summary);
  const hasTemperature = temperature.endsWith('°C');
  const iconUrl = weatherIconUrl(r);
  return <>
    <div className="weather-heading"><strong><svg width="13" height="13" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="M16 8c0 5-6 10-6 10S4 13 4 8a6 6 0 0 1 12 0Z"/><circle cx="10" cy="8" r="2"/></svg>{r.place?.name || '天气查询'}<span className="weather-period">{r.mode === 'current' ? '当前天气' : '天气预报'}</span></strong><span className="weather-date" title={r.timezone}>{r.date}</span></div>
    {!r.ok ? <p role="status">{r.reason || '未取得天气数据。'}</p> : <div className="weather-overview">
      <div className="weather-reading"><div className={`weather-temperature ${hasTemperature ? '' : 'weather-temperature-missing'}`}>
        <strong>{hasTemperature ? temperature.slice(0, -2) : temperature}</strong>{hasTemperature && <span>°C</span>}
      </div><div className="weather-conditions"><span>{summary?.condition || summary?.day_condition || '天气状况暂无数据'}</span>
          {typeof rain === 'number' && Number.isFinite(rain) && rain >= 0 && rain <= 100 && <span className="weather-rain">降水概率 {rain}%</span>}
          {summary?.night_condition && <span className="weather-night">夜间 · {summary.night_condition}</span>}
        </div>
      </div>
      <WeatherSymbol key={iconUrl || summary?.weather_code} code={summary?.weather_code} url={iconUrl}/>
    </div>}
    <div className="weather-footer"><span title={[r.timezone, r.notice].filter(Boolean).join(' · ')}>{safeLink(r.source_url) ? <a href={safeLink(r.source_url)} target="_blank" rel="noreferrer">{r.provider || '天气来源'} ↗</a> : r.provider || '天气来源'}</span>
      {r.queried_at && <time dateTime={r.queried_at}>查询于 {new Date(r.queried_at).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}</time>}
    </div>
  </>;
}

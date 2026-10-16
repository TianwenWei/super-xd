type Place = { place_id: string; place_ref?: string; name: string; address: string; maps_url?: string;
  attributions?: Array<{ provider?: string; providerUri?: string }> };

export type ManualImage = { url: string; caption?: string; page?: number };
export type ManualHit = { chunk_id: number; page: number; section: string; text: string;
  score?: number; source_url?: string; images?: ManualImage[] };
export type LookupResult = {
  ok: boolean; kind?: string; items?: import('./cockpitTypes').MediaItem[]; provider?: string; places?: Place[]; place?: Place;
  query?: string; results?: Array<{ title?: string; url?: string; content?: string }>;
  date?: string; timezone?: string; notice?: string; reason?: string; queried_at?: string;
  mode?: string; source_url?: string;
  details?: { weatherCondition?: { iconBaseUri?: string }; daytimeForecast?: { weatherCondition?: { iconBaseUri?: string } } };
  summary?: { temperature_c?: number; min_c?: number; max_c?: number; condition?: string;
    day_condition?: string; night_condition?: string; rain_probability_percent?: number; weather_code?: number };
  manual?: { id: string; title: string; pages: number; pdf_url?: string; source_url?: string };
  hits?: ManualHit[];
  image_notice?: string;
};
export type LookupRecord = { call_id: string; tool: string; result: LookupResult };

export function safeLink(value?: string): string | undefined {
  try { const url = new URL(value || ''); return url.protocol === 'https:' ? url.href : undefined; }
  catch { return undefined; }
}

export function manualLink(value?: string): string | undefined {
  if (!value) return undefined;
  try {
    if (value.startsWith('/api/manual/')) {
      const url = new URL(value, 'https://manual.local');
      return url.origin === 'https://manual.local' && url.pathname.startsWith('/api/manual/')
        ? url.pathname + url.search + url.hash : undefined;
    }
    const url = new URL(value);
    if (url.username || url.password) return undefined;
    if (url.protocol === 'https:') return url.href;
    return url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname) ? url.href : undefined;
  } catch { return undefined; }
}

export function manualPageLink(value: string | undefined, page: number): string | undefined {
  const url = manualLink(value);
  return url && Number.isInteger(page) && page > 0 ? `${url.split('#')[0]}#page=${page}` : undefined;
}

export function manualImages(hits: ManualHit[]): ManualImage[] {
  const images = new Map<string, ManualImage>();
  for (const hit of hits) for (const image of hit.images || []) {
    const url = manualLink(image.url);
    // Multiple retrieved passages can refer to the same original illustration.
    if (url && !images.has(url)) images.set(url, { ...image, url, page: image.page ?? hit.page });
  }
  return [...images.values()];
}

export function weatherTemperature(summary: LookupResult['summary']): string {
  const valid = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
  if (valid(summary?.temperature_c)) return `${summary.temperature_c}°C`;
  if (valid(summary?.min_c) && valid(summary?.max_c)) return `${summary.min_c}–${summary.max_c}°C`;
  return '温度暂无数据';
}

export function weatherIconUrl(result: LookupResult): string | undefined {
  const base = result.mode === 'current' ? result.details?.weatherCondition?.iconBaseUri
    : result.details?.daytimeForecast?.weatherCondition?.iconBaseUri;
  try {
    const url = new URL(base || '');
    // Google returns the icon's base URI without the format suffix.
    return url.origin === 'https://maps.gstatic.com' && url.pathname.startsWith('/weather/v1/')
      ? `${url.origin}${url.pathname}.svg` : undefined;
  } catch { return undefined; }
}

import { useState } from 'react';
import type { LookupResult, ManualImage } from './lookupViews';
import { manualImages, manualLink, manualPageLink } from './lookupViews';

function ManualFigure({ image, title }: { image: ManualImage; title: string }) {
  const [failed, setFailed] = useState(false);
  const caption = image.caption || (image.page == null ? `${title} · 说明书图片` : `${title} · 第 ${image.page} 页`);
  return <figure className="manual-figure">
    <a href={image.url} target="_blank" rel="noreferrer" aria-label={`${caption}，打开原图`}>
      {failed ? <span className="manual-image-error">图片暂不可用</span>
        : <img src={image.url} alt={caption} loading="lazy" decoding="async" onError={() => setFailed(true)}/>}
    </a>
    <figcaption>{image.caption || (image.page != null ? `第 ${image.page} 页 · 说明书图片` : '说明书图片')}</figcaption>
  </figure>;
}

export default function ManualResults({ result }: { result: LookupResult }) {
  const hits = result.hits || [];
  const images = manualImages(hits);
  const pdf = manualLink(result.manual?.pdf_url);
  const source = manualLink(result.manual?.source_url || result.source_url);
  const title = result.manual?.title || '车辆说明书';
  const pages = [...new Set(hits.map(hit => hit.page))];
  return <>
    <div className="manual-heading"><strong>{title}</strong>{pdf && <a href={pdf} target="_blank" rel="noreferrer">打开说明书 ↗</a>}{!pdf && source && <a href={source} target="_blank" rel="noreferrer">打开资料出处 ↗</a>}</div>
    {images.length === 0 && hits.length > 0 && !result.image_notice && <p className="muted">本次结果未附页图。</p>}
    {images.length > 0 && <div className="manual-images" aria-label="说明书相关图片">{images.map(image => <ManualFigure key={image.url} image={image} title={title}/>)}</div>}
    <details className="manual-excerpts"><summary>查看检索原文{pages.length > 0 && ` · 第 ${pages.join('、')} 页`}</summary>
      {hits.map(hit => <article key={hit.chunk_id}>
        <div><strong>{hit.section || '说明书原文'}</strong>{manualPageLink(pdf, hit.page)
          ? <a href={manualPageLink(pdf, hit.page)} target="_blank" rel="noreferrer">第 {hit.page} 页 ↗</a>
          : <span>第 {hit.page} 页</span>}</div>
        <p>{hit.text}</p>{!pdf && manualLink(hit.source_url) && <a href={manualLink(hit.source_url)} target="_blank" rel="noreferrer">查看原文出处 ↗</a>}
      </article>)}
      {hits.length === 0 && <p>未取得可展示的车书原文。</p>}
    </details>
    {result.image_notice && <p className="manual-image-error">{result.image_notice}</p>}
    {result.notice && <small>{result.notice}</small>}
  </>;
}

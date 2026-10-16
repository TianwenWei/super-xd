import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';

const source = fs.readFileSync(new URL('../src/lookupViews.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { manualLink, manualPageLink, manualImages, weatherTemperature, weatherIconUrl } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);

test('relative manual assets stay inside the manual API after URL normalization', () => {
  assert.equal(manualLink('/api/manual/pages/231.png?v=abc'), '/api/manual/pages/231.png?v=abc');
  for (const url of ['javascript:alert(1)', '//example.com/image.png', '/api/manual/../sessions', '/api/manual/%2e%2e/sessions', undefined]) {
    assert.equal(manualLink(url), undefined);
  }
});

test('source page links use validated page numbers and replace stale PDF fragments', () => {
  assert.equal(manualPageLink('/api/manual/pdf#page=1', 231), '/api/manual/pdf#page=231');
  for (const page of [0, -1, 1.5, NaN]) assert.equal(manualPageLink('/api/manual/pdf', page), undefined);
});

test('repeated passages deduplicate illustrations without discarding different images on the same page', () => {
  const first = { chunk_id: 1, page: 231, section: '无线充电', text: '原文', images: [
    { url: '/api/manual/images/231-1.png', caption: '原始插图' },
    { url: '/api/manual/images/231-2.png', caption: '第二张插图' },
  ] };
  const hits = [first, { ...first, chunk_id: 2 }, { ...first, chunk_id: 3, images: [{ url: 'javascript:alert(1)' }] }];
  const before = JSON.stringify(hits);
  assert.deepEqual(manualImages(hits).map(image => [image.url, image.page]), [
    ['/api/manual/images/231-1.png', 231], ['/api/manual/images/231-2.png', 231],
  ]);
  assert.equal(JSON.stringify(hits), before);
  assert.deepEqual(manualImages([{ ...first, images: undefined }]), []);
});

test('weather preserves zero and negative readings and never fabricates missing temperatures', () => {
  assert.equal(weatherTemperature({ temperature_c: 0 }), '0°C');
  assert.equal(weatherTemperature({ min_c: -5, max_c: 0 }), '-5–0°C');
  assert.equal(weatherTemperature({ temperature_c: 12, min_c: 8, max_c: 16 }), '12°C');
  for (const summary of [undefined, {}, { min_c: 8 }, { temperature_c: NaN }, { min_c: 8, max_c: Infinity }]) {
    assert.equal(weatherTemperature(summary), '温度暂无数据');
  }
});

test('weather uses the matching current or daytime icon only from the weather asset host', () => {
  const details = { weatherCondition: { iconBaseUri: 'https://maps.gstatic.com/weather/v1/clear' },
    daytimeForecast: { weatherCondition: { iconBaseUri: 'https://maps.gstatic.com/weather/v1/rain' } } };
  assert.equal(weatherIconUrl({ ok: true, mode: 'current', details }), 'https://maps.gstatic.com/weather/v1/clear.svg');
  assert.equal(weatherIconUrl({ ok: true, mode: 'forecast', details }), 'https://maps.gstatic.com/weather/v1/rain.svg');
  for (const iconBaseUri of ['https://example.com/weather/v1/rain', 'javascript:alert(1)', 'https://maps.gstatic.com/other/rain']) {
    assert.equal(weatherIconUrl({ ok: true, mode: 'current', details: { weatherCondition: { iconBaseUri } } }), undefined);
  }
});

test('remote manual sources preserve real HTTPS links and explicit loopback CP links without credentials',()=>{
  assert.equal(manualLink('https://manual.example.com/d9.pdf'),'https://manual.example.com/d9.pdf');
  assert.equal(manualLink('http://127.0.0.1:9005/manual/page.png'),'http://127.0.0.1:9005/manual/page.png');
  for(const url of ['https://user:pass@example.com/a.pdf','data:application/pdf,hi','javascript:alert(1)','http://remote.example.com/manual.pdf','//example.com/a.pdf']) assert.equal(manualLink(url),undefined);
  assert.equal(manualPageLink('https://manual.example.com/d9.pdf',3),'https://manual.example.com/d9.pdf#page=3');
  const images=manualImages([{chunk_id:1,page:7,section:'s',text:'source',images:[{url:'https://manual.example.com/page7.png'}]},{chunk_id:2,page:8,section:'s',text:'no image'}]);
  assert.deepEqual(images.map(image=>[image.url,image.page]),[['https://manual.example.com/page7.png',7]]);
  assert.deepEqual(manualImages([{chunk_id:3,page:9,section:'s',text:'no image',images:[]}]),[]);
});

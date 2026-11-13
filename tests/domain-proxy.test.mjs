import test from 'node:test';
import assert from 'node:assert/strict';
import {proxy, UPSTREAM} from '../cloud/domain-proxy/worker.mjs';

test('fixed upstream preserves path, query, cookies and streams a private response', async () => {
  const result = await proxy(new Request('https://istoktext.online/api/reports/abc?q=x', {
    headers: {Cookie: 'istok_beta=owned', 'X-Forwarded-Host': 'evil.example', Forwarded: 'host=evil', 'CF-Connecting-IP': 'spoof'},
  }), async (url, options) => {
    assert.equal(url, UPSTREAM + '/api/reports/abc?q=x');
    assert.equal(options.headers.get('Cookie'), 'istok_beta=owned');
    for (const name of ['Host', 'X-Forwarded-Host', 'Forwarded', 'CF-Connecting-IP']) assert.equal(options.headers.get(name), null);
    assert.equal(options.redirect, 'manual');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.cf, undefined);
    return new Response('private report', {headers: {'Set-Cookie': 'istok_beta=signed; Secure; HttpOnly; SameSite=Lax', 'Cache-Control': 'public', 'Access-Control-Allow-Origin': '*'}});
  });
  assert.equal(await result.text(), 'private report');
  assert.equal(result.headers.get('Cache-Control'), 'no-store');
  assert.match(result.headers.get('Set-Cookie'), /HttpOnly/);
  assert.equal(result.headers.get('Access-Control-Allow-Origin'), null);
});

test('cookie mutations require exact incoming origin before trusted rewrite', async () => {
  let calls = 0;
  const transport = async (_, options) => {calls++; assert.equal(options.headers.get('Origin'), UPSTREAM); assert.equal(new TextDecoder().decode(options.body), '{"own":true}'); return new Response('{}');};
  for (const origin of [null, 'https://evil.example', 'https://www.istoktext.online', 'null']) {
    const headers = origin ? {Origin: origin} : {};
    assert.equal((await proxy(new Request('https://istoktext.online/api/sources', {method:'POST', headers, body:'{}'}), transport)).status, 403);
  }
  assert.equal(calls, 0);
  assert.equal((await proxy(new Request('https://istoktext.online/api/sources', {method:'POST', headers:{Origin:'https://istoktext.online'}, body:'{"own":true}'}), transport)).status, 200);
  assert.equal(calls, 1);
});

test('bearer API can omit Origin; it does not grant internal cookie routes an exemption', async () => {
  const headers = {Authorization: 'Bearer istok_v1_' + 'a'.repeat(32) + '.' + 'b'.repeat(43)};
  const transport = async (_, options) => {assert.equal(options.headers.get('Origin'), null); return new Response('{}');};
  assert.equal((await proxy(new Request('https://istoktext.online/api/v1/checks', {method:'POST', headers, body:'{}'}), transport)).status, 200);
  assert.equal((await proxy(new Request('https://istoktext.online/api/owner/login', {method:'POST', headers, body:'{}'}), transport)).status, 403);
});

test('reject host, scheme, method, cross-site and actual oversize body without forwarding', async () => {
  const transport = () => {throw new Error('must not forward');};
  for (const url of ['https://evil.example/', 'http://istoktext.online/', 'https://istoktext.online:444/']) assert.equal((await proxy(new Request(url), transport)).status, 403);
  assert.equal((await proxy(new Request('https://istoktext.online/', {method:'PUT'}), transport)).status, 405);
  assert.equal((await proxy(new Request('https://istoktext.online/', {method:'POST', headers:{Origin:'https://istoktext.online', 'Sec-Fetch-Site':'cross-site'}}), transport)).status, 403);
  assert.equal((await proxy(new Request('https://istoktext.online/api/import', {method:'POST', headers:{Origin:'https://istoktext.online'}, body:'x'.repeat(2 * 1024 * 1024 + 1)}), transport)).status, 413);
});

test('rewrite only upstream redirects, never follow them with credentials', async () => {
  const request = new Request('https://www.istoktext.online/path');
  const same = await proxy(request, async () => new Response(null, {status:302, headers:{Location:UPSTREAM + '/owner.html'}}));
  assert.equal(same.headers.get('Location'), 'https://www.istoktext.online/owner.html');
  const other = await proxy(request, async () => new Response(null, {status:302, headers:{Location:'https://evil.example/'}}));
  assert.equal(other.status, 502);
  assert.equal((await proxy(request, async () => {throw new Error('secret network detail');})).status, 502);
});

test('opening a link from another site is allowed without granting a mutation', async () => {
  assert.equal((await proxy(new Request('https://istoktext.online/', {headers:{'Sec-Fetch-Site':'cross-site'}}), async () => new Response('home'))).status, 200);
});

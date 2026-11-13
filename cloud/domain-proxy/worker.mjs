// Fixed upstream only. No storage, analytics, credentials, or user-text logging.
export const UPSTREAM = 'https://zhanlaiman--istok-beta.us-east.modal.run';
const HOSTS = new Set(['istoktext.online', 'www.istoktext.online']);
const METHODS = new Set(['GET', 'HEAD', 'POST', 'DELETE']);
const MAX_BODY = 2 * 1024 * 1024;

function reject(message, status) {
  return new Response(message, {status, headers: {'Cache-Control': 'no-store'}});
}

export async function proxy(request, fetchUpstream = fetch) {
  const incoming = new URL(request.url);
  if (incoming.protocol !== 'https:' || !HOSTS.has(incoming.hostname) || incoming.port)
    return reject('Unknown host', 403);
  if (!METHODS.has(request.method)) return reject('Method not allowed', 405);
  const origin = request.headers.get('Origin');
  const bearerAPI = incoming.pathname.startsWith('/api/v1/') &&
    /^Bearer istok_v1_[a-f0-9]{32}\.[A-Za-z0-9_-]{43}$/.test(request.headers.get('Authorization') || '');
  if (origin !== null && origin !== incoming.origin) return reject('Origin rejected', 403);
  if (!['GET', 'HEAD'].includes(request.method) && !origin && !bearerAPI)
    return reject('Origin required', 403);
  if (!['GET', 'HEAD'].includes(request.method) && request.headers.get('Sec-Fetch-Site') === 'cross-site')
    return reject('Cross-site request rejected', 403);

  const target = new URL(UPSTREAM);
  target.pathname = incoming.pathname;
  target.search = incoming.search;
  const headers = new Headers(request.headers);
  for (const name of [...headers.keys()]) {
    if (/^(?:host|forwarded|x-forwarded-.*|x-real-ip|cf-.*|content-length)$/i.test(name)) headers.delete(name);
  }
  if (origin) headers.set('Origin', UPSTREAM);
  let body;
  if (!['GET', 'HEAD'].includes(request.method)) {
    const declared = request.headers.get('Content-Length');
    if (declared && (!/^\d+$/.test(declared) || Number(declared) > MAX_BODY)) return reject('Body too large', 413);
    // Bound the actual stream as well as the declared length; do not trust clients.
    const reader = request.body?.getReader();
    const chunks = [];
    let size = 0;
    if (reader) {
      for (;;) {
        const {value, done} = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_BODY) { await reader.cancel(); return reject('Body too large', 413); }
        chunks.push(value);
      }
    }
    body = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
  }
  let upstream;
  try {
    upstream = await fetchUpstream(target.href, {
      method: request.method, headers, body, redirect: 'manual',
      cf: {cacheTtl: 0, cacheEverything: false},
    });
  } catch {
    return reject('Исток временно недоступен. Повторите открытие страницы; отправленную проверку восстановите по её ID.', 502);
  }
  const outgoing = new Headers(upstream.headers);
  outgoing.set('Cache-Control', 'no-store');
  outgoing.delete('CDN-Cache-Control');
  outgoing.delete('Cloudflare-CDN-Cache-Control');
  for (const name of [...outgoing.keys()]) if (/^access-control-/i.test(name)) outgoing.delete(name);
  const location = outgoing.get('Location');
  if (location) {
    let destination;
    try { destination = new URL(location, target); } catch { return reject('Invalid upstream redirect', 502); }
    if (destination.origin !== UPSTREAM) return reject('External upstream redirect rejected', 502);
    destination.protocol = incoming.protocol;
    destination.host = incoming.host;
    outgoing.set('Location', destination.href);
  }
  return new Response(request.method === 'HEAD' ? null : upstream.body, {
    status: upstream.status, statusText: upstream.statusText, headers: outgoing,
  });
}

export default {fetch: request => proxy(request)};

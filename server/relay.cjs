const TRANSPORT = 'webrtc-relay-v1';
const PROVIDER_ROOT = 'https://rtc.live.cloudflare.com/v1/turn/keys/';
const TURN_TTL_SECONDS = 600;
const REFRESH_MARGIN_MS = 120_000;
const PROVIDER_BODY_MAX = 65_536;

class RelayError extends Error {
  constructor(message, status = 400) {
    super(message);
    this.status = status;
  }
}

function unavailable() {
  return new RelayError('Relay credentials are temporarily unavailable.', 503);
}

function relayIceServers(value) {
  if (!value || !Array.isArray(value.iceServers) || value.iceServers.length > 8) throw unavailable();
  const filtered = [];
  for (const server of value.iceServers) {
    if (!server || typeof server !== 'object') continue;
    const urls = (Array.isArray(server.urls) ? server.urls : [server.urls])
      .filter((url) => typeof url === 'string' && url.length <= 2048 && /^turns?:/i.test(url));
    if (!urls.length || typeof server.username !== 'string'
        || urls.length > 16 || server.username.length > 512
        || typeof server.credential !== 'string' || server.credential.length > 2048
        || !server.username || !server.credential) continue;
    filtered.push({ urls, username: server.username, credential: server.credential });
  }
  if (!filtered.length) throw unavailable();
  return filtered;
}

async function boundedProviderJson(response) {
  if (response.body && typeof response.body.getReader === 'function') {
    const reader = response.body.getReader();
    const chunks = [];
    let size = 0;
    while (true) {
      const {done,value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > PROVIDER_BODY_MAX) { await reader.cancel(); throw unavailable(); }
      chunks.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk,offset); offset += chunk.byteLength; }
    return JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));
  }
  if (typeof response.text === 'function') {
    const text = await response.text();
    if (Buffer.byteLength(text,'utf8') > PROVIDER_BODY_MAX) throw unavailable();
    return JSON.parse(text);
  }
  if (typeof response.json === 'function') return response.json();
  throw unavailable();
}

function createCredentialIssuer(options = {}) {
  const fetcher = options.fetch || globalThis.fetch;
  const clock = options.now || Date.now;
  const keyId = options.keyId === undefined ? process.env.COMP_TURN_KEY_ID : options.keyId;
  const apiToken = options.apiToken === undefined ? process.env.COMP_TURN_API_TOKEN : options.apiToken;
  const maxCacheEntries = Math.max(1, Number(options.maxCacheEntries) || 512);
  const maxIssuesPerMinute = Math.max(1, Number(options.maxIssuesPerMinute) || 6);
  const maxRateEntries = Math.max(1, Number(options.maxRateEntries) || 1024);
  const maxPending = Math.max(1, Number(options.maxPending) || 128);
  const providerTimeoutMs = Math.max(1,Math.min(30_000,Number(options.providerTimeoutMs) || 10_000));
  const cache = new Map();
  const pending = new Map();
  const issued = new Map();

  function noteIssue(accountId, now) {
    const cutoff = now - 60_000;
    const recent = (issued.get(accountId) || []).filter((at) => at > cutoff);
    if (recent.length >= maxIssuesPerMinute) {
      throw new RelayError('Too many relay credential requests.', 429);
    }
    recent.push(now);
    issued.delete(accountId);
    issued.set(accountId, recent);
    while (issued.size > maxRateEntries) issued.delete(issued.keys().next().value);
  }

  function save(accountId, result) {
    cache.delete(accountId);
    cache.set(accountId, result);
    while (cache.size > maxCacheEntries) cache.delete(cache.keys().next().value);
  }

  async function issue(accountId) {
    if (!keyId || !apiToken || typeof fetcher !== 'function') throw unavailable();
    const now = clock();
    const cached = cache.get(accountId);
    if (cached && now < cached.expires_at - REFRESH_MARGIN_MS) {
      cache.delete(accountId);
      cache.set(accountId, cached);
      return cached;
    }
    if (pending.has(accountId)) return pending.get(accountId);
    if (pending.size >= maxPending) throw new RelayError('Too many relay credential requests.',429);
    noteIssue(accountId, now);
    const work = (async () => {
      try {
        const response = await fetcher(
          `${PROVIDER_ROOT}${encodeURIComponent(keyId)}/credentials/generate-ice-servers`,
          {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${apiToken}`,
              'content-type': 'application/json',
            },
            body: JSON.stringify({ ttl: TURN_TTL_SECONDS }),
            signal: AbortSignal.timeout(providerTimeoutMs),
          },
        );
        if (!response || !response.ok) throw unavailable();
        const body = await boundedProviderJson(response);
        const result = {
          ok: true,
          transport: TRANSPORT,
          iceServers: relayIceServers(body),
          expires_at: clock() + TURN_TTL_SECONDS * 1000,
        };
        save(accountId, result);
        return result;
      } catch (err) {
        if (err instanceof RelayError) throw err;
        throw unavailable();
      } finally {
        pending.delete(accountId);
      }
    })();
    pending.set(accountId, work);
    return work;
  }

  return { issue };
}

function createRateLimiter(options = {}) {
  const limit = Math.max(1,Number(options.limit) || 120);
  const windowMs = Math.max(1,Number(options.windowMs) || 60_000);
  const maxEntries = Math.max(1,Number(options.maxEntries) || 2048);
  const clock = options.now || Date.now;
  const entries = new Map();
  function take(key) {
    const now = clock();
    const cutoff = now - windowMs;
    const recent = (entries.get(key) || []).filter((at) => at > cutoff);
    if (recent.length >= limit) throw new RelayError('Too many relay requests.',429);
    recent.push(now);
    entries.delete(key);
    entries.set(key,recent);
    while (entries.size > maxEntries) entries.delete(entries.keys().next().value);
  }
  return {take,size:()=>entries.size};
}

function placeholderAddress(address) {
  const v = String(address || '').toLowerCase();
  return v === '0.0.0.0' || v === '::' || v === '::0' || v === '127.0.0.1' || v === '::1';
}

function validateCandidate(candidate) {
  if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)
      || typeof candidate.candidate !== 'string' || candidate.candidate.length > 4096
      || (candidate.sdpMid !== null && candidate.sdpMid !== undefined
          && (typeof candidate.sdpMid !== 'string' || candidate.sdpMid.length > 128))
      || !Number.isInteger(candidate.sdpMLineIndex) || candidate.sdpMLineIndex < 0
      || candidate.sdpMLineIndex > 32) {
    throw new RelayError('Invalid relay candidate.');
  }
  const parts = candidate.candidate.trim().split(/\s+/);
  const typeAt = parts.indexOf('typ');
  if (!/^candidate:/i.test(parts[0] || '') || typeAt < 0 || parts[typeAt + 1] !== 'relay') {
    throw new RelayError('Only relay candidates are allowed.');
  }
  const raddrAt = parts.indexOf('raddr');
  const rportAt = parts.indexOf('rport');
  if (raddrAt < 0 || !placeholderAddress(parts[raddrAt + 1])) {
    throw new RelayError('Candidate address was not private.');
  }
  if (rportAt < 0 || parts[rportAt + 1] !== '0') {
    throw new RelayError('Candidate port was not private.');
  }
  return {
    candidate: candidate.candidate,
    sdpMid: candidate.sdpMid == null ? null : candidate.sdpMid,
    sdpMLineIndex: candidate.sdpMLineIndex,
  };
}

function validateSdp(sdp) {
  if (typeof sdp !== 'string' || !sdp.length || Buffer.byteLength(sdp, 'utf8') > 65_536
      || /[\u0000]/.test(sdp)) throw new RelayError('Invalid relay session description.');
  const lines = sdp.split(/\r?\n/).filter(Boolean);
  const media = lines.filter((line) => /^m=/i.test(line));
  if (!media.length || media.some((line) => !/^m=application\s/i.test(line))) {
    throw new RelayError('Only data channels are allowed.');
  }
  for (const line of lines) {
    let match;
    if (/^c=/i.test(line)) {
      match = /^c=IN\s+IP(?:4|6)\s+(\S+)$/i.exec(line);
      if (!match || !placeholderAddress(match[1])) throw new RelayError('Session address was not private.');
    }
    if (/^o=/i.test(line)) {
      match = /^o=\S+\s+\S+\s+\S+\s+IN\s+IP(?:4|6)\s+(\S+)$/i.exec(line);
      if (!match || !placeholderAddress(match[1])) throw new RelayError('Session origin was not private.');
    }
    if (/^a=remote-candidates:/i.test(line)) {
      const fields = line.slice(line.indexOf(':') + 1).trim().split(/\s+/);
      if (!fields.length || fields.length % 3 !== 0) throw new RelayError('Invalid remote candidates.');
      for (let i = 0; i < fields.length; i += 3) {
        if (!placeholderAddress(fields[i + 1]) || fields[i + 2] !== '0') {
          throw new RelayError('Remote candidate address was not private.');
        }
      }
    }
    if (/^a=candidate:/i.test(line)) {
      validateCandidate({ candidate: line.slice(2), sdpMid: null, sdpMLineIndex: 0 });
    }
  }
  return sdp;
}

function validateSignal(type, data) {
  if (type === 'candidate') return validateCandidate(data);
  if (type === 'offer' || type === 'answer') return validateSdp(data);
  throw new RelayError('Invalid signal type.');
}

module.exports = {
  TRANSPORT,
  TURN_TTL_SECONDS,
  REFRESH_MARGIN_MS,
  RelayError,
  createCredentialIssuer,
  createRateLimiter,
  validateSignal,
};

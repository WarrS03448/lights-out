'use strict';

// Keep protocol metadata and gameplay fields, never connection headers or the
// attribution library's IP field. Do not retain arbitrary request bodies.
function describeHeaders(headers = {}) {
  const out = {};
  const type = String(headers['content-type'] || '').split(';')[0].trim().toLowerCase();
  if (['application/json', 'text/plain', 'application/octet-stream'].includes(type)) out['content-type'] = type;
  const length = String(headers['content-length'] || '');
  if (/^\d{1,10}$/.test(length)) out['content-length'] = length;
  if (headers.authorization) out.authorization = '[redacted]';
  return out;
}

function describeBody(raw) {
  let body;
  try { body = JSON.parse(String(raw)); } catch { return '[unstructured body omitted]'; }
  if (!body || typeof body !== 'object' || Array.isArray(body)) return '{}';
  const out = {};
  for (const key of ['event_name', 'user_id', 'platform', 'storefront', 'timestamp',
    'first_session_timestamp', 'is_first_game_open']) {
    if (['string', 'number', 'boolean'].includes(typeof body[key])) out[key] = body[key];
  }
  return JSON.stringify(out).slice(0, 4000);
}

module.exports = { describeHeaders, describeBody };

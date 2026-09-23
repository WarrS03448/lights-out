'use strict';

// Proxy origin Host headers identify Railway, not the browser's public callback URL.
// Resolve configuration once so malformed origins fail before serving sign-in links.
function createOriginResolver(env = process.env) {
  let origin;
  if (env.HUB_RECORDING === '1') origin = env.HUB_RECORDING_ORIGIN ?? '';
  else if (env.HUB_PUBLIC_ORIGIN !== undefined) origin = env.HUB_PUBLIC_ORIGIN;
  else if (env.HUB_ACCOUNT_ORIGIN !== undefined) origin = env.HUB_ACCOUNT_ORIGIN;
  else if (env.NODE_ENV === 'production') origin = 'https://lightsoutranked.com';

  if (origin !== undefined) {
    let url;
    try { url = new URL(origin); } catch { throw new Error('Invalid public callback origin'); }
    if (url.protocol !== 'https:' || url.origin !== origin || url.username || url.password) {
      throw new Error('Callback origin must be an exact HTTPS origin without credentials, path, query, or fragment');
    }
    return () => origin;
  }
  // Local development/test servers have no public hostname.
  return req => {
    const proto = String(req.headers['x-forwarded-proto'] || 'https').split(',')[0].trim();
    const host = String(req.headers['x-forwarded-host'] || req.headers.host || '').split(',')[0].trim();
    return `${proto}://${host}`;
  };
}

// Bunny supplies the browser hostname separately from Railway's origin Host.
// This check only restricts redirects/callbacks; it never selects a trusted URL.
function requestUsesOrigin(req, origin) {
  const host=new URL(origin).host.toLowerCase();
  return String(req.headers['x-lightsout-request-host']||req.headers.host||'').toLowerCase()===host;
}

module.exports = { createOriginResolver, requestUsesOrigin };

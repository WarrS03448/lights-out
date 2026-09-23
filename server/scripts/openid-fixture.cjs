'use strict';
// Synthetic, complete assertion. Only Steam's remote signature check is stubbed in tests.
const crypto = require('node:crypto');
function assertion(returnTo, steamId='76561198000000001', nonce) {
  const result = new URL(returnTo);
  const identity = 'https://steamcommunity.com/openid/id/' + steamId;
  const fields = {
    ns:'http://specs.openid.net/auth/2.0', mode:'id_res',
    op_endpoint:'https://steamcommunity.com/openid/login', return_to:returnTo,
    claimed_id:identity, identity, assoc_handle:'test-association', sig:'test-signature',
    response_nonce:nonce || new Date().toISOString().replace(/\.\d{3}Z$/, 'Z') + crypto.randomBytes(12).toString('hex'),
    signed:'op_endpoint,return_to,response_nonce,assoc_handle,claimed_id,identity',
  };
  for(const [name,value] of Object.entries(fields))result.searchParams.set('openid.'+name,value);
  return result;
}
module.exports = {assertion};

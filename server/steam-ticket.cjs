'use strict';

const APP_ID='2406770';
const ENDPOINT='https://api.steampowered.com/ISteamUserAuth/AuthenticateUserTicket/v1/';
const MAX_RESPONSE_BYTES=16384;
const validIdentity=value=>typeof value==='string'&&/^[A-Za-z0-9]{24}$/.test(value);
const validTicket=value=>typeof value==='string'&&value.length>=64&&value.length<=5120&&/^(?:[a-fA-F0-9]{2})+$/.test(value);

/** Server-only verifier. Neither endpoint nor AppID is supplied by the caller.
 * Steam requires the key/ticket in its HTTPS query; never log this URL or preserve
 * transport errors, which can include it. The app sends its ticket to us by POST.
 */
function create({key,fetchImpl=fetch}={}) {
  return async function verify(ticket,identity) {
    if(!key)throw Error('Steam identity verification is unavailable.');
    if(!validTicket(ticket)||!validIdentity(identity))return null;
    try {
      const url=new URL(ENDPOINT);
      url.search=new URLSearchParams({key,appid:APP_ID,ticket,identity}).toString();
      const response=await fetchImpl(url,{redirect:'error',signal:AbortSignal.timeout(8000),
        headers:{accept:'application/json'}});
      if(!response.ok||Number(response.headers.get('content-length'))>MAX_RESPONSE_BYTES) {
        await response.body?.cancel();
        throw Error('Steam response unavailable');
      }
      const chunks=[];let size=0;
      for await(const chunk of response.body) {
        size+=chunk.byteLength;
        if(size>MAX_RESPONSE_BYTES)throw Error('Steam response too large');
        chunks.push(Buffer.from(chunk));
      }
      const result=JSON.parse(Buffer.concat(chunks).toString('utf8'))?.response;
      if(result?.error&&Number.isInteger(result.error.errorcode))return null;
      const params=result?.params;
      if(params?.result!=='OK'||typeof params.steamid!=='string'||!/^\d{17}$/.test(params.steamid))
        throw Error('Invalid Steam response');
      return {steam_id:params.steamid};
    } catch {
      // No error.cause: provider errors and bodies may contain private credentials.
      throw Error('Steam identity verification is unavailable.');
    }
  };
}

module.exports={create,validIdentity,validTicket};

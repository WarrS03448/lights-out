'use strict';
const {randomUUID,randomInt} = require('node:crypto');
const security = require('./account-security.cjs');
const storage = require('./account-store.cjs');
const mail = require('./account-mail.cjs');
const ownership = require('./account-ownership.cjs');
const {DISCONNECT_CONFIRMATION} = ownership;
const ticketModule = require('./steam-ticket.cjs');
const {fail,digest,isSession,isToken} = security;
const SESSION_SECONDS=12*3600;
const LOGIN_SECONDS=600;
const ROOT='/api/auth/account/';
const POST_ROUTES=new Set(['register','verify','login','login/verify','logout','forgot-password','reset-password','change-password',
  'link-steam','link-steam/verify','disconnect-steam','disconnect-steam/verify','game/challenge','game/verify']);
const GET_ROUTES=new Set(['me','game/me']);
const OWNERSHIP_SECONDS=300;
const GAME_CHALLENGE_SECONDS=120;
const isGameSession=value=>typeof value==='string'&&/^lg_[A-Za-z0-9_-]{43}$/.test(value);
const isPlayerId=value=>typeof value==='string'&&/^(?:\d{17}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})$/.test(value);
const ACCEPTED={ok:true,message:'If this email can receive an account code, it will arrive shortly.'};

function configuration(env=process.env) {
  const enabled=env.HUB_ACCOUNTS_ENABLED==='1';
  return {enabled,origin:env.HUB_ACCOUNT_ORIGIN,secret:env.HUB_ACCOUNT_SECRET,gameplay:env.HUB_ACCOUNT_GAMEPLAY_ENABLED==='1',
    ownership:env.HUB_ACCOUNT_OWNERSHIP_ENABLED==='1',
    steamTicket:{key:env.STEAM_WEB_API_KEY},
    durable:Boolean(env.UPSTASH_REDIS_REST_URL&&env.UPSTASH_REDIS_REST_TOKEN),
    sendMail:enabled?mail.fromEnvironment(env):null};
}
function summary(account) {
  return {id:account.id,email:account.email,display_name:account.display_name,email_verified:true,
    player_id:account.player_id||account.id,steam_id:account.steam_id||null,created_at:account.created_at};
}
function create({upstashCmd,prefix,authPrefix=prefix,sendJson,steamIdentity,revokeSteam,ownershipTransaction,options}) {
  const config=options ? {durable:true,...options} : configuration();
  let validOrigin=false;
  try {const url=new URL(config.origin);validOrigin=url.protocol==='https:'&&url.origin===config.origin;}catch{}
  const ready=Boolean(config.enabled&&config.durable&&validOrigin&&typeof config.secret==='string'&&
    Buffer.byteLength(config.secret)>=32&&typeof config.sendMail==='function');
  const db=ready?storage.create({upstashCmd,prefix,authPrefix,secret:config.secret}):null;
  const allowed=account=>Boolean(account&&(!config.allowEmail||config.allowEmail(account.email)));
  const gameAllowed=id=>!config.allowGame||config.allowGame(id);
  const verifyTicket=ticketModule.create(config.steamTicket);
  function requireReady() {if(!ready)fail(503,'accounts_unavailable','Lights Out accounts are not available yet.');}
  const bearer=req=>String(req.headers.authorization||'').startsWith('Bearer ')?String(req.headers.authorization).slice(7):'';
  async function rate(action,subject,limit=10,seconds=900) {
    const retry=await db.rate([['global:'+action,120,60],[action+':'+subject,limit,seconds]]);
    if(retry) {const error=new security.AccountError(429,'rate_limited','Too many attempts. Try again later.');error.retry=retry;throw error;}
  }
  async function authenticated(token) {
    requireReady();
    if(isGameSession(token))return null;
    if(isSession(token)) {
      const session=await db.get('session',digest(token));
      if(!session)return null;
      const remembered=session.remember_me===true&&session.expires_at===null;
      if(!remembered&&(!Number.isFinite(session.expires_at)||session.expires_at<=Date.now()))return null;
      let account=await db.get('user',session.account_id);
      if(account?.steam_id&&!account.player_id) {
        await ownership.readSteam({upstashCmd,prefix},account.steam_id);
        account=await db.get('user',session.account_id);
      }
      return allowed(account)&&account.version===session.version?account:null;
    }
    // Optional linkage makes Steam a second independent way to open the same account.
    const steam=token?await steamIdentity(token):null;
    if(!steam?.account_id)return null;
    const account=await db.get('user',steam.account_id);
    return allowed(account)&&account.steam_id===steam.steam_id&&account.player_id===steam.player_id?account:null;
  }
  async function gameIdentity(token) {
    requireReady();
    if(!config.gameplay||!isGameSession(token))return null;
    const child=await db.get('game-session',digest(token));
    const now=Date.now();
    if(!child||!isPlayerId(child.account_id)||!isPlayerId(child.player_id)||
      !Number.isSafeInteger(child.version)||child.version<1||
      typeof child.parent_hash!=='string'||!/^[a-f0-9]{64}$/.test(child.parent_hash)||
      typeof child.game_steam_id!=='string'||!/^\d{17}$/.test(child.game_steam_id)||
      !Number.isSafeInteger(child.issued_at)||child.issued_at>now||
      !Number.isSafeInteger(child.expires_at)||child.expires_at<=now||
      child.expires_at>child.issued_at+SESSION_SECONDS*1000)return null;
    const parent=await db.get('session',child.parent_hash);
    if(!parent||parent.account_id!==child.account_id||parent.version!==child.version)return null;
    if(!(parent.remember_me===true&&parent.expires_at===null)&&
      (!Number.isFinite(parent.expires_at)||parent.expires_at<=Date.now()))return null;
    const account=await db.get('user',child.account_id);
    if(!allowed(account)||!gameAllowed(child.game_steam_id)||account.version!==child.version||(account.player_id||account.id)!==child.player_id)return null;
    return {player_id:child.player_id,game_steam_id:child.game_steam_id,steam_id:child.game_steam_id,
      account_id:account.id,auth_method:'lightsout',persona:account.display_name,avatar:''};
  }
  async function requireAccount(req) {
    const account=await authenticated(bearer(req));
    if(!account)fail(401,'not_signed_in','Sign in to your Lights Out account.');
    return account;
  }
  async function markUsed(token, expected, readOnly=false) {
    const current=await gameIdentity(token);
    if(!current || current.player_id!==expected.player_id || current.game_steam_id!==expected.game_steam_id)return false;
    const child=await db.get('game-session',digest(token));
    if(!child || child.player_id!==expected.player_id || child.game_steam_id!==expected.game_steam_id)return false;
    const account=await db.get('user',child.account_id);
    if(!account)return false;
    const linked=account.steam_id||'';
    const base=prefix+'accounts:';
    if(readOnly)return await upstashCmd(['EVAL',ownership.CHECK_GAME,'3',base+'user:'+child.account_id,
      base+'game-session:'+digest(token),base+'session:'+child.parent_hash,JSON.stringify(child),String(Date.now())],
      {strict:true,timeout:5000})===1;
    return await upstashCmd(['EVAL',ownership.MARK_GAME,'5',base+'user:'+child.account_id,
      base+'game-session:'+digest(token),base+'session:'+child.parent_hash,
      base+'steam-identity:'+linked,base+'steam:'+linked,JSON.stringify(child),String(Date.now()),linked],
      {strict:true,timeout:5000})===1;
  }
  async function body(req) {
    if(req.headers.origin && req.headers.origin!==config.origin)fail(403,'invalid_origin','Request origin is not allowed.');
    if(!/^application\/json(?:\s*;|$)/i.test(req.headers['content-type']||''))fail(415,'json_required','Send JSON.');
    if(Number(req.headers['content-length'])>8192)fail(413,'body_too_large','Request is too large.');
    const chunks=[];let size=0;
    for await(const chunk of req) {size+=chunk.length;if(size>8192)fail(413,'body_too_large','Request is too large.');chunks.push(chunk);}
    let parsed;try {parsed=JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch {fail(400,'invalid_json','Invalid JSON.');}
    if(!parsed||typeof parsed!=='object'||Array.isArray(parsed))fail(400,'invalid_json','Expected an object.');
    return parsed;
  }
  async function deliver(kind,email,record,seconds,actionable=true) {
    const token=security.randomToken();
    await db.set(kind,digest(token),{...record,actionable},seconds);
    try {await config.sendMail({kind,to:email,token,actionable});}
    catch {await db.remove(kind,digest(token));throw Error('Account mail unavailable');}
  }
  async function handle(req,res,action) {
    requireReady();
    if((action.startsWith('link-steam')||action.startsWith('disconnect-steam'))&&!config.ownership)
      fail(503,'ownership_unavailable','Account linking and disconnecting are not available yet.');
    if(action.startsWith('game/')&&!config.gameplay)fail(503,'gameplay_unavailable','Account gameplay is not available yet.');
    if(action==='game/me') {
      const identity=await gameIdentity(bearer(req));
      if(!identity)fail(401,'game_verification_required','Verify your active game identity again.');
      return sendJson(res,200,{ok:true,...identity});
    }
    if(action==='me') {
      const account=await requireAccount(req);
      return sendJson(res,200,{ok:true,account:summary(account),account_id:account.id,
        auth_method:isSession(bearer(req))?'lightsout':'steam',steam_id:account.steam_id||null,
        persona:account.display_name,avatar:''});
    }
    const input=await body(req);
    if(action==='register'||action==='forgot-password') {
      const email=security.email(input.email);await rate('mail',email,3,3600);
      if(config.allowEmail&&!config.allowEmail(email))return sendJson(res,202,ACCEPTED);
      const account=await db.byEmail(email);
      // Always take the storage + delivery path. A non-actionable request sends
      // a helpful notice to the address owner, never a usable credential. The
      // public response cannot expose existence via SMTP latency or failure.
      if(action==='register')await deliver('verify',email,{email},3600,!account);
      if(action==='forgot-password')await deliver('reset',email,
        {account_id:account?.id||null,version:account?.version||0},900,Boolean(account));
      return sendJson(res,202,ACCEPTED);
    }
    if(action==='verify') {
      if(!isToken(input.token))fail(400,'invalid_code','This code is invalid or expired.');
      const name=security.displayName(input.display_name);security.password(input.password);
      await rate('verify',digest(input.token));
      const pending=await db.get('verify',digest(input.token));
      if(!pending||!pending.actionable||!allowed(pending))fail(400,'invalid_code','This code is invalid or expired.');
      const accountId=randomUUID();
      const account={id:accountId,player_id:accountId,profile_used:false,email:pending.email,display_name:name,
        password_hash:await security.hashPassword(input.password),version:1,steam_id:null,created_at:Date.now(),updated_at:Date.now()};
      if(!await db.createAccount(input.token,pending,account))fail(400,'invalid_code','This code is invalid or expired.');
      return sendJson(res,201,{ok:true,account:summary(account)});
    }
    if(action==='login') {
      const email=security.email(input.email);await rate('login',email);
      if(config.allowEmail&&!config.allowEmail(email))fail(401,'invalid_credentials','Email or password is incorrect.');
      if(input.remember_me!==undefined&&typeof input.remember_me!=='boolean')fail(400,'invalid_remember_choice','Choose whether to remember this sign-in.');
      const account=await db.byEmail(email);
      const valid=await security.checkPassword(input.password,account?.password_hash);
      if(!account||!valid)fail(401,'invalid_credentials','Email or password is incorrect.');
      await rate('login-mail',account.id,5,900);
      const challenge=security.randomToken(),code=security.loginCode();
      const state={account_id:account.id,version:account.version,remember_me:input.remember_me===true,
        code_hash:security.codeDigest(config.secret,challenge,code),attempts:0,expires_at:Date.now()+LOGIN_SECONDS*1000};
      if(!await db.issueLoginChallenge(account,challenge,state,LOGIN_SECONDS))fail(401,'invalid_credentials','Email or password is incorrect.');
      try {await config.sendMail({kind:'login',to:account.email,token:code,actionable:true});}
      catch {await db.remove('login',digest(challenge));throw Error('Login email unavailable');}
      return sendJson(res,202,{ok:true,status:'verification_required',challenge,expires_in:LOGIN_SECONDS});
    }
    if(action==='login/verify') {
      if(!isToken(input.challenge)||typeof input.code!=='string'||!/^\d{6}$/.test(input.code))
        fail(401,'invalid_login_code','The login code is invalid or expired.');
      await rate('login-verify',digest(input.challenge),10,600);
      const pending=await db.get('login',digest(input.challenge));
      const account=pending?await db.get('user',pending.account_id):null;
      if(!allowed(account))fail(401,'invalid_login_code','The login code is invalid or expired.');
      const token='lo_'+security.randomToken();
      const remembered=pending.remember_me===true;
      const session={account_id:account.id,version:account.version,remember_me:remembered,
        expires_at:remembered?null:Date.now()+SESSION_SECONDS*1000};
      const codeHash=security.codeDigest(config.secret,input.challenge,input.code);
      const result=await db.completeLogin(input.challenge,account,codeHash,token,session,SESSION_SECONDS);
      if(result===-2)fail(409,'remembered_session_limit','Too many remembered devices. Sign out on another device and try again.');
      if(result!==1)
        fail(401,'invalid_login_code','The login code is invalid or expired.');
      return sendJson(res,200,{ok:true,token,remember_me:remembered,
        expires_in:remembered?null:SESSION_SECONDS,account:summary(account)});
    }
    if(action==='logout') {await signout(bearer(req));return sendJson(res,200,{ok:true});}
    if(action==='reset-password') {
      if(!isToken(input.token))fail(400,'invalid_code','This code is invalid or expired.');
      security.password(input.password);await rate('reset',digest(input.token));
      const reset=await db.get('reset',digest(input.token));
      const account=reset?.actionable?await db.get('user',reset.account_id):null;
      if(!allowed(account)||account.version!==reset.version)fail(400,'invalid_code','This code is invalid or expired.');
      const hash=await security.hashPassword(input.password);
      if(!await db.changePassword(account,hash,Date.now(),{token:input.token,record:reset}))
        fail(400,'invalid_code','This code is invalid or expired.');
      return sendJson(res,200,{ok:true});
    }
    const account=await requireAccount(req);await rate('sensitive',account.id);
    if(action==='game/challenge'||action==='game/verify') {
      if(!isSession(bearer(req)))fail(401,'lightsout_verification_required','Sign in to your Lights Out account.');
      if(action==='game/challenge') {
        const alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
        const identity=Array.from({length:24},()=>alphabet[randomInt(alphabet.length)]).join('');
        const challenge=security.randomToken();
        const state={kind:'game',account_id:account.id,version:account.version,parent_hash:digest(bearer(req)),
          identity,expires_at:Date.now()+GAME_CHALLENGE_SECONDS*1000};
        if(!await db.issueGame(account,bearer(req),challenge,state,GAME_CHALLENGE_SECONDS))
          fail(401,'account_changed','Sign in and try again.');
        return sendJson(res,202,{ok:true,challenge,identity,expires_in:GAME_CHALLENGE_SECONDS});
      }
      if(!isToken(input.challenge)||!ticketModule.validTicket(input.ticket))
        fail(401,'invalid_game_proof','Your active game identity could not be verified.');
      const pending=await db.get('game-challenge',digest(input.challenge));
      if(!pending||pending.account_id!==account.id||pending.version!==account.version||
        pending.parent_hash!==digest(bearer(req))||pending.expires_at<=Date.now())
        fail(401,'invalid_game_proof','Your active game identity could not be verified.');
      const verified=await verifyTicket(input.ticket,pending.identity);
      if(verified&&!gameAllowed(verified.steam_id))fail(403,'private_game_identity','This game account is not invited to the private test.');
      if(!verified)fail(401,'invalid_game_proof','Your active game identity could not be verified.');
      const parent=await db.get('session',digest(bearer(req)));
      if(!parent)fail(401,'account_changed','Sign in and try again.');
      const issued=Date.now();
      const expires=Math.min(issued+SESSION_SECONDS*1000,parent.expires_at??Infinity);
      const token='lg_'+security.randomToken();
      const child={account_id:account.id,version:account.version,parent_hash:digest(bearer(req)),
        player_id:account.player_id||account.id,game_steam_id:verified.steam_id,issued_at:issued,expires_at:expires};
      if(!await db.finishGame(account,bearer(req),input.challenge,pending.identity,token,child))
        fail(401,'invalid_game_proof','Your active game identity could not be verified.');
      return sendJson(res,200,{ok:true,token,player_id:child.player_id,game_steam_id:child.game_steam_id,
        expires_in:Math.max(0,Math.floor((expires-Date.now())/1000))});
    }
    if(action==='change-password') {
      security.password(input.password);
      if(!await security.checkPassword(input.current_password,account.password_hash))fail(401,'invalid_credentials','Current password is incorrect.');
      const hash=await security.hashPassword(input.password);
      if(!await db.changePassword(account,hash,Date.now()))fail(409,'account_changed','Account changed. Sign in and try again.');
      return sendJson(res,200,{ok:true});
    }
    if(action==='link-steam'||action==='disconnect-steam') {
      if(!isSession(bearer(req)))fail(401,'lightsout_verification_required','Sign in with your Lights Out email and password first.');
      const valid=await security.checkPassword(input.password,account.password_hash);
      if(!valid)fail(401,'invalid_credentials','Verify your Lights Out password.');
      let steamId=account.steam_id,steamHash=null;
      if(action==='link-steam') {
        const steam=typeof input.steam_token==='string'&&!isSession(input.steam_token)?await steamIdentity(input.steam_token):null;
        if(!steam||!Number.isFinite(steam.authenticated_at)||steam.authenticated_at>Date.now()||
          steam.authenticated_at<Date.now()-OWNERSHIP_SECONDS*1000)
          fail(401,'fresh_steam_required','Sign in with Steam again to confirm this link.');
        steamId=steam.steam_id;steamHash=digest(input.steam_token);
      }
      if(!steamId)fail(409,'steam_not_linked','There is no Steam sign-in to disconnect.');
      await rate('ownership-mail',account.id,5,900);
      const ledger=await ownership.readSteam({upstashCmd,prefix},steamId);
      const challenge=security.randomToken(),code=security.loginCode();
      const state={kind:action,account_id:account.id,version:account.version,parent_hash:digest(bearer(req)),
        steam_id:steamId,steam_hash:steamHash,steam_generation:ledger?.generation||0,
        code_hash:security.codeDigest(config.secret,action+':'+challenge,code),attempts:0,
        expires_at:Date.now()+OWNERSHIP_SECONDS*1000,
        confirmation:action==='disconnect-steam'?DISCONNECT_CONFIRMATION.id:null};
      if(!await db.issueOwnership(account,bearer(req),challenge,state,OWNERSHIP_SECONDS))
        fail(401,'account_changed','Account changed. Sign in and try again.');
      try {await config.sendMail({kind:action,to:account.email,token:code,steam_id:steamId,actionable:true});}
      catch {await db.remove('ownership',digest(challenge));throw Error('Ownership email unavailable');}
      return sendJson(res,202,{ok:true,status:'verification_required',challenge,expires_in:OWNERSHIP_SECONDS,
        steam_id:steamId,...(action==='disconnect-steam'?{confirmation:DISCONNECT_CONFIRMATION}:{})});
    }
    if(action==='link-steam/verify'||action==='disconnect-steam/verify') {
      const kind=action.slice(0,-7);
      if(!isSession(bearer(req))||!isToken(input.challenge)||typeof input.code!=='string'||!/^\d{6}$/.test(input.code))
        fail(401,'invalid_action_code','This confirmation code is invalid or expired.');
      const pending=await db.get('ownership',digest(input.challenge));
      if(!pending||pending.kind!==kind||pending.account_id!==account.id||pending.parent_hash!==digest(bearer(req)))
        fail(401,'invalid_action_code','This confirmation code is invalid or expired.');
      if(kind==='disconnect-steam'&&input.confirmation!==DISCONNECT_CONFIRMATION.id)
        fail(400,'confirmation_required','Confirm that progress will remain with your Lights Out account.');
      if(kind==='link-steam'&&(typeof input.steam_token!=='string'||digest(input.steam_token)!==pending.steam_hash))
        fail(401,'invalid_action_code','This confirmation code is invalid or expired.');
      const finish=()=>db.finishOwnership(account,bearer(req),{...input,kind},pending,
        security.codeDigest(config.secret,kind+':'+input.challenge,input.code),randomUUID());
      const result=ownershipTransaction ? await ownershipTransaction({account,pending},finish) : await finish();
      if(result===-2)fail(409,'progress_conflict','Both accounts have separate player data. Their progress cannot be combined automatically.');
      if(result===-1)fail(409,'link_conflict','This account association has changed or belongs to another account.');
      if(result!==1)fail(401,'invalid_action_code','This confirmation code is invalid or expired.');
      return sendJson(res,200,{ok:true,status:'sign_in_required',account:summary(await db.get('user',account.id))});
    }
  }
  async function route(req,res,method,pathname) {
    if(!pathname.startsWith(ROOT))return false;
    const action=pathname.slice(ROOT.length);
    if(!GET_ROUTES.has(action)&&!POST_ROUTES.has(action))return false;
    if(method!==(GET_ROUTES.has(action)?'GET':'POST')) {sendJson(res,405,{ok:false,error:'Method not allowed.'},{allow:GET_ROUTES.has(action)?'GET':'POST'});return true;}
    try {await handle(req,res,action);}
    catch(error) {
      const known=error instanceof security.AccountError;
      sendJson(res,known?error.status:503,{ok:false,code:known?error.code:'account_service_unavailable',
        error:known?error.message:'Account service is temporarily unavailable.'},error.retry?{'retry-after':String(error.retry)}:{});
    }
    return true;
  }
  async function signout(token) {
    requireReady();
    if(isSession(token)) {
      const session=await db.get('session',digest(token));
      if(session)await db.revokeSession(token,session.account_id);
    }
    else if(token&&revokeSteam)await revokeSteam(token);
  }
  return {route,authenticated,gameIdentity,markUsed,signout,ready,summary,bySteam:async steamId=>{
    requireReady();const account=await db.bySteam(steamId);return allowed(account)?account:null;
  }};
}
module.exports={create,isSession,isGameSession,summary,configuration};

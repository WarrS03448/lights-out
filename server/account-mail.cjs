'use strict';

function fromEnvironment(env=process.env) {
  const method=env.HUB_MAIL_TRANSPORT||'smtp';
  if(!['smtp','resend-https'].includes(method))return null;
  const port=Number(env.HUB_SMTP_PORT||465);
  if(!env.HUB_SMTP_HOST || !env.HUB_SMTP_USER || !env.HUB_SMTP_PASSWORD ||
      !env.HUB_SMTP_FROM || /[\r\n]/.test(env.HUB_SMTP_FROM) || ![465,587].includes(port)) return null;
  // Resend's SMTP password is its restricted sending API key. Reuse that same
  // server-only secret over HTTPS on hosts that do not permit SMTP egress.
  if(method==='resend-https'&&(env.HUB_SMTP_HOST!=='smtp.resend.com'||env.HUB_SMTP_USER!=='resend'))return null;
  const transport=method==='smtp'?require('nodemailer').createTransport({
    host:env.HUB_SMTP_HOST,port,secure:port===465,requireTLS:true,
    auth:{user:env.HUB_SMTP_USER,pass:env.HUB_SMTP_PASSWORD},
    tls:{minVersion:'TLSv1.2',rejectUnauthorized:true},
    connectionTimeout:10000,greetingTimeout:10000,socketTimeout:15000,
    logger:false,debug:false,disableFileAccess:true,disableUrlAccess:true,
  }):null;
  return async({to,kind,token,steam_id,actionable=true})=>{
    const verification=kind==='verify';
    const login=kind==='login';
    const ownership=kind==='link-steam'||kind==='disconnect-steam';
    const action=kind==='link-steam'?'link':'disconnect';
    const text=actionable
      ? (login?'Your Lights Out login code is:':verification?'Your Lights Out email verification code is:':'Your Lights Out password reset code is:')+
        '\n\n'+token+'\n\nEnter this code '+(verification?'in the Lights Out app or on the Lights Out website':'in Lights Out')+'. It expires in '+(login?'10 minutes':verification?'one hour':'15 minutes')+
        '. If you did not request this, ignore this email. Never share this code.\n'
      : (verification?'You already have a Lights Out account at this email address. Sign in, or request a password reset.':
        'There is no Lights Out account registered to this email address. You can create one in the Lights Out app or on the Lights Out website.')+
        '\n\nIf you did not request this email, ignore it.\n';
    const ownershipText=ownership?
      'Confirm '+action+' of Steam account '+steam_id+' for your Lights Out account ('+to+').'+
      (kind==='disconnect-steam'?' Your progress and account data will stay with your Lights Out account. Steam sign-in will no longer give access to this data.':' This changes which sign-in methods can access your player data.')+
      '\n\nYour confirmation code is: '+token+'\n\nIt expires in 5 minutes. Only enter it for this action in Lights Out. Never share this code. If you did not request this change, do not enter the code.\n':null;
    const message={
      from:env.HUB_SMTP_FROM,to,
      subject:ownership?'Confirm Steam '+action+' for Lights Out':actionable?(login?'Your Lights Out login code':verification?'Create your Lights Out account':'Reset your Lights Out password'):'Your Lights Out account request',
      text:ownershipText||text,
      disableFileAccess:true,disableUrlAccess:true,
    };
    if(method==='resend-https') {
      try {
        const response=await fetch('https://api.resend.com/emails',{
          method:'POST',redirect:'error',signal:AbortSignal.timeout(10000),
          headers:{authorization:'Bearer '+env.HUB_SMTP_PASSWORD,'content-type':'application/json'},
          body:JSON.stringify({from:message.from,to:[to],subject:message.subject,text:message.text}),
        });
        if(!response.ok)throw Error('delivery rejected');
        const result=await response.json();
        if(!result||typeof result.id!=='string'||!result.id||result.id.length>128)throw Error('missing receipt');
        return;
      } catch {throw Error('Account email was not accepted');}
    }
    const result=await transport.sendMail(message);
    if(!Array.isArray(result.accepted)||!result.accepted.some(address=>String(address).toLowerCase()===to))
      throw Error('Account email was not accepted');
  };
}
module.exports={fromEnvironment};

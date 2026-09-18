'use strict';
(() => {
  const strings = window.LightsOutAccountStrings;
  const language = document.getElementById('language');
  const emailForm = document.getElementById('email-form');
  const verifyForm = document.getElementById('verify-form');
  const complete = document.getElementById('complete');
  const status = document.getElementById('status');
  let busy = false, statusKey = '';
  const browserLanguage = (navigator.language || 'en').split('-')[0];
  language.value = Object.hasOwn(strings, browserLanguage) ? browserLanguage : 'en';
  const copy = key => (strings[language.value] || strings.en)[key] || strings.en[key];
  function translate() {
    document.documentElement.lang = language.value;
    document.title = copy('create') + ' · Lights Out';
    document.querySelectorAll('[data-copy]').forEach(node => {node.textContent = copy(node.dataset.copy);});
    status.textContent = statusKey ? copy(statusKey) : '';
  }
  function announce(key) {
    statusKey = key;
    status.textContent = key ? copy(key) : '';
    if (key) status.focus();
  }
  function setBusy(value) {
    busy = value;
    for (const form of [emailForm, verifyForm]) {
      form.querySelector('fieldset').disabled = value;
      form.setAttribute('aria-busy', String(value));
    }
  }
  function showVerification() {
    emailForm.hidden = true;
    verifyForm.hidden = false;
    document.getElementById('code').focus();
  }
  async function request(action, payload) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch('/api/auth/account/' + action, {
        method: 'POST', headers: {'content-type': 'application/json', 'accept': 'application/json'},
        credentials: 'omit', cache: 'no-store', redirect: 'error', referrerPolicy:'no-referrer',
        body: JSON.stringify(payload), signal: controller.signal,
      });
      if (response.status === 503) throw 'unavailable';
      const body = await response.json();
      if (!response.ok || !body.ok) {
        if (response.status === 429) throw 'limited';
        if (response.status === 403) throw 'origin';
        const known = {invalid_email:'badEmail',invalid_password:'badPassword',invalid_display_name:'badName',invalid_code:'badCode'};
        throw known[body.code] || 'failed';
      }
      if (response.status !== (action === 'register' ? 202 : 201)) throw 'failed';
    } finally { clearTimeout(timer); }
  }
  emailForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    setBusy(true); announce('sending');
    try {
      await request('register', {email: document.getElementById('email').value});
      announce('sent');
      showVerification();
    } catch (key) { announce(typeof key === 'string' ? key : 'failed'); }
    finally {setBusy(false); if (!verifyForm.hidden) document.getElementById('code').focus();}
  });
  verifyForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    const password = document.getElementById('password');
    const confirm = document.getElementById('confirm-password');
    if (password.value !== confirm.value) {announce('mismatch'); return;}
    if ([...password.value].length < 6 || [...password.value].length > 128) {announce('badPassword'); return;}
    const payload = {token:document.getElementById('code').value.trim(),
      password:password.value, display_name:document.getElementById('display-name').value};
    password.value = confirm.value = '';
    setBusy(true); announce('creating');
    try {
      await request('verify', payload);
      verifyForm.reset(); emailForm.reset(); verifyForm.hidden = true; complete.hidden = false;
      announce('ready');
    } catch (key) { announce(typeof key === 'string' ? key : 'uncertain'); }
    finally {payload.password = ''; payload.token = ''; setBusy(false);}
  });
  document.getElementById('have-code').addEventListener('click', () => {if (!busy) {announce(''); showVerification();}});
  document.getElementById('restart').addEventListener('click', () => {
    if (busy) return;
    verifyForm.reset(); verifyForm.hidden = true; emailForm.hidden = false;
    announce(''); document.getElementById('email').focus();
  });
  language.addEventListener('change', translate);
  window.addEventListener('pagehide', () => {verifyForm.reset(); emailForm.reset();});
  translate(); setBusy(false);
})();

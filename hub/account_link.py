"""Settings-only linking: isolated proof sessions, no temporary gameplay adoption."""
import threading
import webbrowser
from . import auth, i18n


def _spawn(work):
    threading.Thread(target=work, daemon=True).start()


class AccountLink:
    def __init__(self, session, *, request=None, start=None, wait=None,
                 open_browser=None, spawn=None, revoke=None):
        self.session = session
        self.request = request or auth.account_request
        self.start = start or auth.start
        self.wait = wait or auth.wait_for
        self.open_browser = open_browser or webbrowser.open
        self.spawn = spawn or _spawn
        self.revoke = revoke or auth.revoke_session
        self.epoch = 0
        self.stage = ''
        self.busy = False
        self.error = ''
        self.email = ''
        self._email_token = self._steam_token = ''
        self._login_challenge = self._challenge = self._url = ''
        self._origin_token = self._steam_id = ''
        self._origin_epoch = 0
        self._recovery_challenge = self._reset_token = self._recovery_email = ''

    def eligible(self):
        s = self.session
        parent = getattr(s, 'parent_token', '') or getattr(s, 'token', '')
        return bool(s.me and s.token and not parent.startswith(('lo_', 'lg_')) and
                    not s.me.get('linked_account') and not getattr(s, 'party', None) and s.phase == 'idle' and
                    not s.locked_in() and not getattr(s.panel, '_closed', False))

    @property
    def active(self):
        return self.stage not in ('', 'done', 'uncertain')

    def _current(self, epoch):
        return (self.epoch == epoch and self.active and self.eligible() and
                self.session.token == self._origin_token and
                self.session._account_epoch == self._origin_epoch)

    def _dispose(self, token):
        if token and token != self._origin_token:
            self.spawn(lambda: self.revoke(token))

    def cancel(self, notify=True):
        self.epoch += 1
        tokens = (self._email_token, self._steam_token)
        self.stage = ''; self.busy = False; self.error = ''; self.email = ''
        self._email_token = self._steam_token = ''
        self._login_challenge = self._challenge = self._url = ''
        self._recovery_challenge = self._reset_token = self._recovery_email = ''
        for token in tokens:
            self._dispose(token)
        if notify:
            self.session._changed()

    def view(self):
        if self.active and not self._current(self.epoch):
            self.cancel(notify=False)
        # No password, email code, bearer token or server challenge enters a snapshot.
        return {'stage': self.stage, 'busy': self.busy, 'error': self.error,
                'email': self.email, 'steam_id': self._steam_id, 'epoch': self.epoch,
                'can_connect': self.eligible() and not self.active}

    def _finish(self, uncertain=False):
        self.cancel(notify=False)
        self.session.finish_account_link()
        self.stage = 'uncertain' if uncertain else 'done'

    def _task(self, work, apply, final=False, recovery=''):
        epoch = self.epoch
        self.busy = True; self.error = ''; self.session._changed()
        def worker():
            try:
                result = work()
            except Exception as exc:
                code = getattr(exc, 'code', '')
                status = getattr(exc, 'status', None)
                ambiguous = final and (status is None or status >= 500 or code in ('account_changed', 'not_signed_in'))
                allowed = {'progress_conflict', 'link_conflict', 'active_match',
                    'fresh_steam_required', 'invalid_action_code', 'ownership_unavailable',
                    'account_changed', 'not_signed_in', 'invalid_credentials',
                    'rate_limited', 'wrong_steam', 'already_linked', 'invalid_code', 'invalid_email', 'invalid_password'}
                code = code if code in allowed else 'unavailable'
                def failed():
                    if self._current(epoch):
                        if ambiguous:
                            self._finish(uncertain=True)
                        else:
                            self.busy = False; self.error = code
                            if recovery:
                                self.error = {'rate_limited':'recovery_limited', 'invalid_code':'recovery_invalid',
                                    'invalid_email':'recovery_email', 'invalid_password':'recovery_password'}.get(code,
                                    'reset_uncertain' if recovery == 'reset-password' else 'recovery_failed')
                                if recovery == 'reset-password' and code == 'invalid_code':
                                    self._reset_token = ''; self.stage = 'forgot_password'
                        self.session._changed()
                    elif self.epoch == epoch:
                        self.cancel()
                self.session.panel.post(failed)
                return
            def delivered():
                if not self._current(epoch):
                    self._dispose(result.get('token') if isinstance(result, dict) else '')
                    if self.epoch == epoch:
                        self.cancel()
                    return
                self.busy = False
                try:
                    apply(result)
                except (KeyError, TypeError, ValueError):
                    self._dispose(result.get('token') if isinstance(result, dict) else '')
                    if final:
                        self._finish(uncertain=True)
                    else:
                        self.error = 'unavailable'
                self.session._changed()
            self.session.panel.post(delivered)
        self.spawn(worker)

    def action(self, action, fields=None):
        fields = fields if isinstance(fields, dict) else {}
        if action == 'start':
            if not self.eligible() or self.active:
                return
            self.cancel(notify=False)
            self._origin_token = self.session.token
            self._origin_epoch = self.session._account_epoch
            me = self.session.me or {}
            self._steam_id = str(me.get('game_steam_id') or me.get('steam_id') or '')
            if len(self._steam_id) != 17 or not self._steam_id.isdigit():
                return
            self.stage = 'login'; self.session._changed(); return
        if action == 'restart' and not self.busy:
            self.cancel(notify=False)
            self.action('start')
            return
        if action == 'cancel':
            # Confirmation may already be committed on the server. Wait for its result.
            if self.stage == 'confirm' and self.busy:
                return
            self.cancel(); return
        if action == 'open_steam' and self.stage == 'steam' and self._url:
            self.open_browser(self._url); return
        if not self._current(self.epoch) or self.busy:
            return
        if action == 'recover' and self.stage in ('login', 'password'):
            self.cancel(notify=False)
            self.stage = 'forgot_password'; self.session._changed(); return
        if action == 'forgot-password' and self.stage in ('forgot_password', 'recovery_code'):
            email = self._recovery_email if self.stage == 'recovery_code' else fields.get('email')
            payload = {'email': email, 'language': i18n.get_language()}
            def applied(result):
                self._recovery_challenge = result['challenge']; self._recovery_email = email
                self.stage = 'recovery_code'
            self._task(lambda: self.request(action, payload), applied, recovery=action)
            return
        if action == 'forgot-password/verify' and self.stage == 'recovery_code' and self._recovery_challenge:
            payload = {'challenge': self._recovery_challenge, 'code': str(fields.get('code') or '').strip()}
            def work():
                try: return self.request(action, payload)
                finally: payload.clear()
            def applied(result):
                self._reset_token = result['reset_token']; self._recovery_challenge = ''
                self.stage = 'reset_password'
            self._task(work, applied, recovery=action)
            return
        if action == 'reset-password' and self.stage == 'reset_password' and self._reset_token:
            payload = {'token': self._reset_token, 'password': fields.get('password')}
            def work():
                try: return self.request(action, payload)
                finally: payload.clear()
            def applied(result):
                self._reset_token = self._recovery_challenge = self._recovery_email = ''
                self.stage = 'login'; self.error = 'password_reset'
            self._task(work, applied, recovery=action)
            return
        if action == 'login' and self.stage == 'login':
            payload = {'email': fields.get('email'), 'password': fields.get('password'), 'remember_me': False}
            def applied(result):
                self._login_challenge = result['challenge']; self.stage = 'login_code'
            self._task(lambda: self.request('login', payload), applied)
        elif action == 'login_code' and self.stage == 'login_code':
            payload = {'challenge': self._login_challenge, 'code': str(fields.get('code') or '').strip()}
            def applied(result):
                self._email_token = result['token']
                account = result.get('account') or {}
                self.email = str(account.get('email') or '')
                self._login_challenge = ''
                if account.get('steam_id'):
                    self.error = 'already_linked'
                    self.stage = 'login'
                    self._dispose(self._email_token); self._email_token = ''
                else:
                    self.stage = 'steam'
            self._task(lambda: self.request('login/verify', payload), applied)
        elif action == 'steam' and self.stage == 'steam':
            epoch = self.epoch
            def work():
                started = self.start()
                if not self._current(epoch):
                    return {}
                self._url = started['url']
                self.open_browser(self._url)
                proof = self.wait(started['code'], should_stop=lambda: not self._current(epoch))
                if str(proof.get('game_steam_id') or proof.get('steam_id') or '') != self._steam_id:
                    self._dispose(proof.get('token'))
                    raise auth.AuthError('Steam account mismatch.', code='wrong_steam')
                return proof
            def applied(result):
                self._steam_token = result['token']; self._url = ''; self.stage = 'password'
            self._task(work, applied)
        elif action == 'password' and self.stage == 'password':
            payload = {'password': fields.get('password'), 'steam_token': self._steam_token}
            parent = self._email_token
            def applied(result):
                if result.get('steam_id') != self._steam_id:
                    self.error = 'wrong_steam'; return
                self._challenge = result['challenge']; self.stage = 'confirm'
            self._task(lambda: self.request('link-steam', payload, parent), applied)
        elif action == 'confirm' and self.stage == 'confirm':
            payload = {'challenge': self._challenge, 'code': str(fields.get('code') or '').strip(),
                       'steam_token': self._steam_token}
            parent = self._email_token
            def applied(result):
                if result.get('status') != 'sign_in_required':
                    self._finish(uncertain=True); return
                # Ownership changed atomically. Existing credentials cannot be reused.
                self._finish()
            self._task(lambda: self.request('link-steam/verify', payload, parent), applied, final=True)

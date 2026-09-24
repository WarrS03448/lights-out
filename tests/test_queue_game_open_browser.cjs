// Run: NODE_PATH=<Playwright packages> HUB_TEST_PYTHON=<Python> node tests/test_queue_game_open_browser.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const fixture = JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON || 'python', ['-c',
  'import json; from tests.test_hub import _web_panel; from hub.webui.snapshot import state_snapshot,strings_for; from hub.i18n import CODES; p,s=_web_panel(); s.phase="lobby"; print(json.dumps(dict(state=state_snapshot(s,p),languages={lang:strings_for(lang) for lang in CODES})))'
], {cwd: root, encoding: 'utf8'}));
const state = fixture.state;
state.view = 'competitive'; state.update = null; state.gamemode_update = null;
state.comp.phase = 'idle';
state.settings.game.running = true;
state.comp.lobby.i_am_coin_captain = true;
state.comp.lobby.i_am_captain = true;
state.comp.lobby.coin.i_won_toss = true;
state.comp.lobby.i_pick_side = true;
state.comp.lobby.chat_epoch = 1;
state.comp.lobby.messages = [];
state.auth.player_id = '00000000-0000-4000-8000-000000000010';
(async () => {
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  try {
    const page = await browser.newPage(), errors = [], verbs = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('http://hub.test/**', route => {
      const u = new URL(route.request().url());
      if (u.pathname === '/state') return route.fulfill({json: state});
      if (u.pathname === '/events') return route.fulfill({json: []});
      if (u.pathname.startsWith('/verb/')) {
        verbs.push({path: u.pathname, args: route.request().postDataJSON()});
        return route.fulfill({json: {ok: true}});
      }
      const file = path.join(root, 'hub/webui/static', u.pathname === '/' ? 'index.html' : decodeURIComponent(u.pathname));
      return fs.existsSync(file) ? route.fulfill({path: file}) : route.fulfill({status: 404, body: ''});
    });
    await page.goto('http://hub.test/'); await page.waitForSelector('.hero-action');
    const push = () => page.evaluate(s => __hub.onState(s), state);
    for (const [lang, strings] of Object.entries(fixture.languages)) {
      state.lang = lang; state.strings = strings;
      for (const width of [800, 1050, 1440]) for (const mode of ['BB1', 'BB5']) {
        await page.setViewportSize({width, height: width === 800 ? 560 : 720});
        state.comp.mode_id = mode; state.comp.phase = 'idle'; await push();
        assert.equal(await page.locator('.comp-readiness-item').count(), 2);
        assert(await page.locator('.btn-find').isEnabled());
        await page.locator('.btn-find').click();
        state.comp.phase = 'queued'; await push();
        const reminder = page.locator('.search-reminder');
        const expected = lang === 'en'
          ? 'You may have Bodycam open while searching, but to join a Lights Out match, the game must be closed during the pre-match selection phase'
          : strings.comp_search_game_notice;
        assert.equal(await reminder.textContent(), expected);
        assert(await reminder.evaluate(n => n.scrollWidth <= n.clientWidth + 1));
        if (process.env.HUB_TEST_SCREENSHOTS && lang === 'en' && width === 1050 && mode === 'BB1') {
          fs.mkdirSync(process.env.HUB_TEST_SCREENSHOTS, {recursive: true});
          await page.screenshot({path: path.join(process.env.HUB_TEST_SCREENSHOTS, 'queue-notice.png')});
        }
        assert.equal(await page.locator('.lobby-game-notice').count(), 0);
        state.comp.phase = 'lobby';
        for (const stage of ['coin', 'flipping', 'choice', 'side', 'veto', 'ready', 'rejoin']) {
          state.comp.lobby.stage = stage; state.comp.lobby.stage_seconds = 20; await push();
          const notice = page.locator('.lobby-game-notice');
          assert.equal(await notice.textContent(), lang === 'en' ? 'If Bodycam is open, close it now' : strings.comp_lobby_close_game);
          assert(await notice.evaluate(n => {
            const r = n.getBoundingClientRect();
            return r.left >= 0 && r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1 && n.scrollWidth <= n.clientWidth + 1;
          }), [lang, width, mode, stage].join('/'));
          const input = page.locator('.chat-row input');
          await input.fill('private draft');
          await input.evaluate(n => {n.setSelectionRange(2, 6); window.savedComposer = n;});
          for (let n = 0; n < 3; n++) {
            state.comp.lobby.stage_seconds--; state.comp.online++;
            state.comp.lobby.messages.push({channel: 'all', name: 'Player', text: 'Update ' + n});
            state.comp.lobby.messages = state.comp.lobby.messages.slice(-20);
            await push();
          }
          assert(await input.evaluate(n => n === savedComposer && n === document.activeElement && n.value === 'private draft' && n.selectionStart === 2 && n.selectionEnd === 6));
          assert.equal(await notice.count(), 1);
          if (process.env.HUB_TEST_SCREENSHOTS && lang === 'en' && width === 1050 && mode === 'BB1' && stage === 'side') {
            await page.screenshot({path: path.join(process.env.HUB_TEST_SCREENSHOTS, 'lobby-notice.png')});
          }
          if (['coin', 'choice', 'side'].includes(stage)) {
            const choice = page.locator('.lobby-action .btn-choice').first();
            await choice.focus();
            await choice.evaluate(n => {window.savedChoice = n;});
            state.comp.lobby.stage_seconds--; await push();
            assert(await choice.evaluate(n => n === savedChoice && n === document.activeElement), stage + ' keeps selection focus');
            const verb = {coin: '/pick_coin', choice: '/choose', side: '/choose_side'}[stage];
            const sent = page.waitForRequest(r => r.url().endsWith('/verb' + verb));
            await page.keyboard.press('Enter');
            const request = await sent;
            assert.deepEqual(request.postDataJSON(), [{coin: 'heads', choice: 'side', side: 'attack'}[stage]]);
          }
        }
        for (const phase of ['connecting', 'live', 'idle']) {
          state.comp.phase = phase; await push();
          assert.equal(await page.locator('.lobby-game-notice,.search-reminder').count(), 0);
        }
      }
    }
    state.comp.phase = 'lobby'; state.comp.lobby.stage = 'side'; await push();
    await page.locator('.chat-row input').fill('old account draft');
    state.auth.player_id = '00000000-0000-4000-8000-000000000011'; await push();
    assert.equal(await page.locator('.chat-row input').inputValue(), '');
    state.auth.signed_in = false; await push();
    assert.equal(await page.locator('.lobby-game-notice').count(), 0);
    assert(verbs.some(v => v.path.endsWith('/find_match')));
    assert(verbs.some(v => v.path.endsWith('/pick_coin')));
    assert.deepEqual(errors, []);
    console.log('Open-game queue UI: 7 languages, 3 widths, both modes, all lobby stages, changed snapshots, draft/caret/focus and account reset passed.');
  } finally { await browser.close(); }
})().catch(e => {console.error(e); process.exitCode = 1;});

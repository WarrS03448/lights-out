// UTF-8. Run with NODE_PATH pointing at the bundled Playwright dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const python = process.env.HUB_TEST_PYTHON || 'python';
const fixtureScript = String.raw`
import json
from tests.test_hub import _web_panel
from tests.test_screen_history import _record
from hub import i18n
from hub.webui.snapshot import state_snapshot
from hub.webui.screens import history as H, postmatch as P

attack = '<img src=x onerror="window.__combatAttack=1">'
complete = {
    'version': 1, 'status': 'complete',
    'coverage': {'damage': True, 'shots': True, 'objectives': True},
    'enemyDamage': 1234, 'friendlyDamage': 12, 'damageTaken': 900,
    'assists': 6, 'headshots': 4, 'shots': 80, 'hits': 41,
    'adr': 123.4, 'accuracy': 51.25,
    'playerStats': [{'steam_id': '4' * 17, 'damageDealt': 900, 'damageTaken': 120},
                    {'steam_id': '2' * 17, 'damageDealt': 12, 'damageTaken': None}],
    'weaponStats': [{'weapon': attack, 'enemyDamage': 900, 'friendlyDamage': 12,
                     'kills': 8, 'headshots': 3, 'shots': 60, 'hits': 32}],
}
partial = {
    'version': 1, 'status': 'partial',
    'coverage': {'damage': True, 'shots': False, 'objectives': False},
    'enemyDamage': 245, 'friendlyDamage': None, 'damageTaken': None,
    'assists': 0, 'headshots': None, 'shots': None, 'hits': None,
    'adr': None, 'accuracy': None, 'weaponStats': [],
}
zero = {
    'version': 1, 'status': 'complete',
    'coverage': {'damage': True, 'shots': True, 'objectives': True},
    'enemyDamage': 0, 'friendlyDamage': 0, 'damageTaken': 0,
    'assists': 0, 'headshots': 0, 'shots': 0, 'hits': 0,
    'adr': 0, 'accuracy': 0, 'weaponStats': [],
}
players = [
    {'steam_id': '1' * 17, 'persona': 'Complete Player', 'team': 1, 'connected': True},
    {'steam_id': '2' * 17, 'persona': 'Partial Player', 'team': 1, 'connected': True},
    {'steam_id': '3' * 17, 'persona': 'Zero Player', 'team': 2, 'connected': True},
    {'steam_id': '4' * 17, 'persona': attack, 'team': 2, 'connected': True},
]
scoreboard = [
    {'steam_id': '1' * 17, 'team': 1, 'reported': True, 'kills': 10, 'deaths': 3,
     'team_kills': 0, 'combat': complete},
    {'steam_id': '2' * 17, 'team': 1, 'reported': True, 'kills': 7, 'deaths': 5,
     'team_kills': 0, 'combat': partial},
    {'steam_id': '3' * 17, 'team': 2, 'reported': True, 'kills': 0, 'deaths': 0,
     'team_kills': 0, 'combat': zero},
    {'steam_id': '4' * 17, 'team': 2, 'reported': True, 'kills': 2, 'deaths': 4,
     'team_kills': 0},
]
record = _record(players=players, teams={'1': ['1' * 17, '2' * 17],
                                         '2': ['3' * 17, '4' * 17]},
                 scoreboard=scoreboard)
panel, session = _web_panel()
session.history = []
state = state_snapshot(session, panel)
post_record = {
    'match_id': 'm1', 'map': 'Rome', 'won': True, 'winner': 1, 'score': [7, 3],
    'my_team': 1, 'teams': {
        1: [{'steam_id': '1' * 17, 'name': 'Complete Player', 'is_me': True,
             'combat': complete},
            {'steam_id': '2' * 17, 'name': 'Partial Player'}],
        2: [{'steam_id': '3' * 17, 'name': 'Zero Player'},
            {'steam_id': '4' * 17, 'name': attack}],
    },
    'scoreboard': scoreboard,
}
print(json.dumps({
    'state': state,
    'history': H._detail(record, '1' * 17),
    'postmatch': P._card(post_record),
    'languages': {code: {'shared': i18n.STRINGS[code], 'history': H.strings_for(code),
                         'postmatch': P.strings_for(code)} for code in i18n.CODES},
}))
`;
const fixture = JSON.parse(execFileSync(python, ['-c', fixtureScript], {
  cwd: root, encoding: 'utf8'
}));

function playerRow(page, prefix, name) {
  return page.locator('.' + prefix + '-player.sb').filter({hasText: name}).first();
}

async function assertCombatCases(page, prefix, strings) {
  const complete = playerRow(page, prefix, 'Complete Player');
  const partial = playerRow(page, prefix, 'Partial Player');
  const zero = playerRow(page, prefix, 'Zero Player');
  const unavailable = page.locator('.' + prefix + '-player.sb').filter({hasText: '<img src=x'}).first();

  assert.equal((await complete.locator('.combat-status').textContent()).trim(), strings.combat_complete);
  assert.equal((await partial.locator('.combat-status').textContent()).trim(), strings.combat_partial);
  assert.equal((await unavailable.locator('.combat-status').textContent()).trim(), strings.combat_unavailable);

  let toggle = complete.locator('.combat-toggle');
  let panel = page.locator('#' + await toggle.getAttribute('aria-controls'));
  await toggle.click();
  await panel.waitFor();
  assert.equal(await panel.locator('[data-stat="damage"] .combat-label').textContent(), strings.col_damage);
  assert.equal(await panel.locator('[data-stat="friendly-damage"] .combat-label').textContent(), strings.col_friendly_damage);
  assert.equal(await panel.locator('[data-stat="damage-taken"] .combat-label').textContent(), strings.col_damage_taken);
  assert.equal(await panel.locator('[data-stat="headshots"] .combat-label').textContent(), strings.col_headshots);
  assert.equal(await panel.locator('.combat-weapons-title').textContent(), strings.player_damage);
  assert.deepEqual(await panel.locator('.combat-weapons th').allTextContents(),
    [strings.damage_player, strings.damage_to, strings.damage_from]);
  assert.equal(await panel.locator('[data-stat="damage"] .combat-value').textContent(), '1234');
  assert.equal(await panel.locator('[data-stat="friendly-damage"] .combat-value').textContent(), '12');
  assert.equal(await panel.locator('[data-stat="damage-taken"] .combat-value').textContent(), '900');
  assert.equal(await panel.locator('[data-stat="adr"] .combat-value').textContent(), '123.4');
  assert.equal(await panel.locator('[data-stat="assists"] .combat-value').textContent(), '6');
  assert.equal(await panel.locator('[data-stat="headshots"] .combat-value').textContent(), '4');
  assert.equal(await panel.locator('[data-stat="accuracy"] .combat-value').textContent(), '51.3%');
  assert.equal(await panel.locator('.combat-weapon-name').first().textContent(),
    '<img src=x onerror="window.__combatAttack=1">');
  assert.deepEqual(await panel.locator('.combat-weapons tbody tr').first().locator('td').allTextContents(),
    ['<img src=x onerror="window.__combatAttack=1">', '900', '120']);
  assert.deepEqual(await panel.locator('.combat-weapons tbody tr').nth(1).locator('td').allTextContents(),
    ['Partial Player', '12', strings.stat_none]);
  assert.equal(await panel.locator('img,svg,script').count(), 0, 'display strings stay text nodes');
  assert.equal(await page.evaluate(() => window.__combatAttack), undefined);

  toggle = zero.locator('.combat-toggle');
  panel = page.locator('#' + await toggle.getAttribute('aria-controls'));
  await toggle.click();
  assert.equal(await panel.locator('[data-stat="damage"] .combat-value').textContent(), '0');
  assert.equal(await panel.locator('[data-stat="friendly-damage"] .combat-value').textContent(), '0');
  assert.equal(await panel.locator('[data-stat="damage-taken"] .combat-value').textContent(), '0');
  assert.equal(await panel.locator('[data-stat="adr"] .combat-value').textContent(), '0');
  assert.equal(await panel.locator('[data-stat="assists"] .combat-value').textContent(), '0');
  assert.equal(await panel.locator('[data-stat="headshots"] .combat-value').textContent(), '0');
  assert.equal(await panel.locator('[data-stat="accuracy"] .combat-value').textContent(), '0%');

  toggle = partial.locator('.combat-toggle');
  panel = page.locator('#' + await toggle.getAttribute('aria-controls'));
  await toggle.click();
  assert.equal(await panel.locator('.combat-note').first().textContent(), strings.combat_partial_note);
  assert.equal(await panel.locator('.combat-note').last().textContent(), strings.player_damage_unavailable);
  assert.equal(await panel.locator('[data-stat="damage"] .combat-value').textContent(), '245');
  assert.equal(await panel.locator('[data-stat="friendly-damage"] .combat-value').textContent(), strings.stat_none);
  assert.equal(await panel.locator('[data-stat="damage-taken"] .combat-value').textContent(), strings.stat_none);
  assert.equal(await panel.locator('[data-stat="adr"] .combat-value').textContent(), strings.stat_none);

  toggle = unavailable.locator('.combat-toggle');
  panel = page.locator('#' + await toggle.getAttribute('aria-controls'));
  await toggle.click();
  assert.equal(await panel.locator('.combat-note').textContent(), strings.combat_unavailable_note);
  assert.equal(await page.evaluate(() => window.__combatAttack), undefined);
}

(async () => {
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {channel: 'msedge'})
  });
  try {
    const state = fixture.state;
    state.view = 'history';
    state.update = null;
    state.gamemode_update = null;
    state.history.open_id = 'm1';
    state.history.open = fixture.history;
    state.history.open_loading = false;
    state.postmatch = {open: false, card: null, strings: fixture.languages.en.postmatch};

    const page = await browser.newPage({viewport: {width: 1200, height: 760}});
    const errors = [], calls = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://hub.test/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/state') return route.fulfill({json: state});
      if (url.pathname === '/events') return route.fulfill({json: []});
      if (url.pathname.startsWith('/verb/')) {
        calls.push([url.pathname.split('/').pop(), ...JSON.parse(route.request().postData() || '[]')]);
        return route.fulfill({json: {ok: true}});
      }
      const file = path.join(root, 'hub/webui/static',
        url.pathname === '/' ? 'index.html' : decodeURIComponent(url.pathname));
      return fs.existsSync(file) ? route.fulfill({path: file}) : route.fulfill({status: 404, body: ''});
    });
    await page.goto('http://hub.test/');
    await page.waitForSelector('.md-overlay');
    await assertCombatCases(page, 'md', fixture.languages.en.history);

    for (const [code, words] of Object.entries(fixture.languages)) {
      state.strings = words.shared;
      state.history.strings = words.history;
      await page.setViewportSize({width: code === 'ru' ? 520 : 800, height: 700});
      await page.evaluate(next => window.__hub.onState(next), state);
      await page.waitForSelector('.md-overlay');
      const partial = playerRow(page, 'md', 'Partial Player');
      assert.equal((await partial.locator('.combat-status').textContent()).trim(), words.history.combat_partial);
      const fits = await page.locator('.md-overlay .ui-modal').evaluate(node => {
        const rect = node.getBoundingClientRect();
        return rect.left >= 0 && rect.right <= innerWidth + 1 && rect.width > 0;
      });
      assert(fits, code + ' history combat modal fits');
    }

    state.history.open_id = '';
    state.history.open = null;
    state.strings = fixture.languages.en.shared;
    state.history.strings = fixture.languages.en.history;
    state.postmatch = {open: true, card: fixture.postmatch, strings: fixture.languages.en.postmatch};
    await page.setViewportSize({width: 1200, height: 760});
    await page.evaluate(next => window.__hub.onState(next), state);
    await page.waitForSelector('.pm-overlay');
    await assertCombatCases(page, 'pm', fixture.languages.en.postmatch);
    if (process.env.HUB_COMBAT_SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.HUB_COMBAT_SCREENSHOT_DIR, {recursive: true});
      const expanded = page.locator('.pm-overlay .combat-toggle[aria-expanded="true"]');
      while (await expanded.count() > 1) await expanded.nth(1).click();
      await page.setViewportSize({width: 1200, height: 900});
      await page.locator('.pm-overlay .ui-modal').evaluate(node => { node.scrollTop = 0; });
      await page.screenshot({
        path: path.join(process.env.HUB_COMBAT_SCREENSHOT_DIR, 'combat-postmatch-wide.png'),
        fullPage: true
      });
    }

    await page.setViewportSize({width: 420, height: 700});
    await page.evaluate(next => window.__hub.onState(next), state);
    const postFits = await page.locator('.pm-overlay .ui-modal').evaluate(node => {
      const rect = node.getBoundingClientRect();
      return rect.left >= 0 && rect.right <= innerWidth + 1 && rect.width > 0;
    });
    assert(postFits, 'narrow post-match combat modal fits');
    if (process.env.HUB_COMBAT_SCREENSHOT_DIR) {
      await page.setViewportSize({width: 420, height: 900});
      await page.locator('.pm-overlay .ui-modal').evaluate(node => { node.scrollTop = 0; });
      await page.screenshot({
        path: path.join(process.env.HUB_COMBAT_SCREENSHOT_DIR, 'combat-postmatch-narrow.png'),
        fullPage: true
      });
    }

    const warningAckCount = () => calls.filter(call => call[0] === 'ack_combat_warning').length;
    const startingAckCount = warningAckCount();
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', {
        configurable: true, value: 'hidden'
      });
      window.__hub.onEvent({
        type: 'combat_warning', text: 'Hidden warning.', ms: 8000,
        match_id: 'm-warning', warning_id: 'w-hidden'
      });
      delete document.visibilityState;
    });
    await page.waitForTimeout(50);
    assert.equal(warningAckCount(), startingAckCount,
      'warning rendered while document is hidden is not acknowledged');

    await page.evaluate(() => {
      Object.defineProperty(document, 'hasFocus', {
        configurable: true, value: () => false
      });
      window.__hub.onEvent({
        type: 'combat_warning', text: 'Background warning.', ms: 8000,
        match_id: 'm-warning', warning_id: 'w-unfocused'
      });
      delete document.hasFocus;
    });
    await page.waitForTimeout(50);
    assert.equal(warningAckCount(), startingAckCount,
      'warning rendered in an unfocused document is not acknowledged');

    await page.evaluate(() => {
      const originalToast = HubUI.toast;
      HubUI.toast = (text, ms) => {
        originalToast(text, ms);
        document.getElementById('toast').style.transform = 'translateY(-10000px)';
      };
      window.__hub.onEvent({
        type: 'combat_warning', text: 'Offscreen warning.', ms: 8000,
        match_id: 'm-warning', warning_id: 'w-offscreen'
      });
      HubUI.toast = originalToast;
      document.getElementById('toast').style.transform = '';
    });
    await page.waitForTimeout(50);
    assert.equal(warningAckCount(), startingAckCount,
      'warning outside the viewport is not acknowledged');

    await page.evaluate(() => window.__hub.onEvent({
      type: 'combat_warning', text: 'Stop attacking teammates.', ms: 8000,
      match_id: 'm-warning', warning_id: 'w-1'
    }));
    await page.waitForFunction(() => {
      const toast = document.getElementById('toast');
      return toast && toast.textContent === 'Stop attacking teammates.';
    });
    await page.waitForTimeout(50);
    assert(calls.some(call => JSON.stringify(call) === JSON.stringify(
      ['ack_combat_warning', 'm-warning', 'w-1'])), 'visible warning is acknowledged');
    const ackCount = warningAckCount();
    await page.evaluate(() => window.__hub.onEvent({
      type: 'combat_warning', text: 'Stop attacking teammates.', ms: 8000,
      match_id: 'm-warning', warning_id: 'w-1', seq: 99
    }));
    await page.waitForTimeout(50);
    assert.equal(warningAckCount(), ackCount,
      'push plus poll replay acknowledges once');
    assert.deepEqual(errors, []);
    console.log('Combat scoreboards: complete, partial, zero, unavailable, escaping, damage sources, 7 languages and responsive layouts.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });

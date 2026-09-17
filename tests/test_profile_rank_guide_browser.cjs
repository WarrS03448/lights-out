// Run with Playwright available: node tests/test_profile_rank_guide_browser.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const ranks = require('../server/progress.cjs').ranks();
const fixture = JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON || 'python', ['-c',
  'import json; from tests.test_hub import _web_panel; from hub.webui.snapshot import state_snapshot; from hub.i18n import STRINGS; from hub.webui.screens.profile import _strings_for; p,s=_web_panel(); print(json.dumps(dict(snapshot=state_snapshot(s,p),languages={k:dict(shared=STRINGS[k],profile=_strings_for(k)) for k in STRINGS})))'
], {cwd: root, encoding: 'utf8'}));
const s = fixture.snapshot;
s.view = 'profile'; s.update = null; s.gamemode_update = null;
s.comp.ladder = {ranks, placement_matches: 5};
s.auth.rank = {rank: 5, rank_name: ranks.names[4], division: 2, rr: 42, top: false};
s.auth.placing = false;
s.history.asked = true;

(async function () {
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {channel: 'msedge'})});
  try {
    const page = await browser.newPage({viewport: {width: 1200, height: 760}});
    const errors = [], calls = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://hub.test/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/state') return route.fulfill({json: s});
      if (url.pathname === '/events') return route.fulfill({json: []});
      if (url.pathname.startsWith('/verb/')) {
        const verb = url.pathname.split('/').pop(), args = JSON.parse(route.request().postData() || '[]');
        calls.push([verb, ...args]);
        if (verb === 'set_view') s.view = args[0];
        return route.fulfill({json: {ok: true}});
      }
      const file = path.join(root, 'hub/webui/static', url.pathname === '/' ? 'index.html' : decodeURIComponent(url.pathname));
      return fs.existsSync(file) ? route.fulfill({path: file}) : route.fulfill({status: 404, body: ''});
    });
    async function push() { s.history.seq++; await page.evaluate(state => __hub.onState(state), s); }
    await page.goto('http://hub.test/');
    await page.waitForSelector('.profile');
    await page.locator('#nav [data-view="competitive"]').click();
    await page.waitForFunction(() => document.querySelector('#nav .active').dataset.view === 'competitive');
    const compOpener = page.locator('button.rank-guide-open');
    assert.equal(await compOpener.count(), 1, 'A placed competitive rank is clickable');
    const originalPhase = s.comp.phase;
    s.auth.level = 5;
    for (const phase of ['idle', 'queued']) {
      s.comp.phase = phase; await push();
      assert.equal(await page.locator('.hero > .ghost').count(), 0, 'No level-number watermark while ' + phase);
    }
    s.comp.phase = originalPhase; await push();
    for (const action of ['click', 'Enter', ' ']) {
      if (action === 'click') await compOpener.click(); else await compOpener.press(action);
      await page.waitForSelector('.profile-ranks');
      assert.deepEqual(await page.locator('.profile-ranks-division-rr').allTextContents(),
        [...Array(21).fill('0–99 RR'), '0–99 RR', '100–199 RR', '200+ RR']);
      assert.equal(await page.locator('.profile-ranks-capstone .profile-ranks-rr').textContent(), '300+ RR');
      assert.equal(await page.locator('.profile-ranks [aria-current="step"] .rank-badge use').getAttribute('href'), '#rk-14');
      assert.equal(await page.locator('.profile-ranks-division .rank-badge, .profile-ranks-capstone .rank-badge').count(), 25);
      await push();
      assert.equal(await page.locator('.profile-ranks').count(), 1, 'Competitive guide survives state updates');
      await page.locator('.profile-ranks-back').click();
      await page.waitForSelector('button.rank-guide-open');
      assert.equal(await compOpener.evaluate(n => n === document.activeElement), true, 'Back returns focus to the Competitive badge');
      assert.equal(await page.locator('#nav .active').getAttribute('data-view'), 'competitive');
    }
    s.comp.phase = 'queued'; await push();
    await compOpener.click();
    s.comp.phase = 'found'; await push();
    assert.equal(await page.locator('.profile-ranks').count(), 0, 'A found match takes priority over the rank guide');
    assert.equal(await page.locator('.found-box').count(), 1);
    s.comp.phase = originalPhase;
    s.auth.placing = true; await push();
    assert.equal(await compOpener.count(), 0, 'Placement players cannot open a stale rank');
    assert.equal(await page.locator('.rank-emblem').count(), 0);
    const earnedRank = s.auth.rank;
    s.auth.placing = false; s.auth.rank = null; await push();
    assert.equal(await compOpener.count(), 0, 'Unranked players have no clickable rank');
    s.auth.rank = earnedRank; await push();
    await compOpener.click();
    s.auth.placing = true; await push();
    assert.equal(await page.locator('.profile-ranks').count(), 0, 'Losing a rank closes the Competitive guide');
    s.auth.placing = false; await push();
    await page.locator('#nav [data-view="profile"]').click();
    await page.waitForSelector('.profile');
    const opener = page.locator('.profile-id-badge button');
    assert.equal(await opener.count(), 1, 'The profile rank icon opens the rank guide');
    assert.equal(await page.locator('.profile-tabs').count(), 0, 'Profile renders directly without a Friends tab');
    for (const action of ['click', 'Enter', ' ']) {
      if (action === 'click') await opener.click(); else await opener.press(action);
      await page.waitForSelector('.profile-ranks');
      assert.equal(await page.locator('.profile').count(), 0, 'Guide is a separate profile screen');
      assert.deepEqual(await page.locator('.profile-ranks-card h2').allTextContents(), [...ranks.names, ranks.top]);
      const symbols = await page.locator('.profile-ranks-division .rank-badge use, .profile-ranks-capstone .rank-badge use').evaluateAll(nodes => nodes.map(n => n.getAttribute('href')));
      assert.deepEqual(symbols, Array.from({length: 25}, (_, i) => '#rk-' + String(i + 1).padStart(2, '0')));
      assert.equal(await page.locator('.profile-ranks [aria-current="step"]').count(), 1);
      assert.equal(await page.locator('.profile-ranks [aria-current="step"] .rank-badge use').getAttribute('href'), '#rk-14');
      assert.match(await page.locator('.profile-ranks [aria-current="step"]').textContent(), /You are here/);
      const scroll = await page.locator('.profile-ranks-scroll').evaluate(n => { n.scrollTop = 180; n.dispatchEvent(new Event('scroll')); return n.scrollTop; });
      await push();
      assert.equal(await page.locator('.profile-ranks').count(), 1, 'State pushes keep the guide open');
      assert.equal(await page.locator('.profile-ranks-scroll').evaluate(n => n.scrollTop), scroll, 'Guide scroll survives redraw');
      await page.getByRole('button', {name: 'Back to profile', exact: true}).click();
      await page.waitForSelector('.profile');
      assert.equal(await opener.evaluate(n => n === document.activeElement), true, 'Returning restores focus to rank button');
    }
    await opener.click();
    s.comp.ladder.ranks = {...ranks, rr_per_division: 50, top_at: 225, top_slots: 75};
    await push();
    assert.deepEqual(await page.locator('.profile-ranks-division-rr').allTextContents(),
      [...Array(21).fill('0–49 RR'), '0–49 RR', '50–99 RR', '100+ RR'], 'RR ranges use the server configuration');
    assert.equal(await page.locator('.profile-ranks-capstone .profile-ranks-rr').textContent(), '225+ RR');
    const reaperRules = await page.locator('.profile-ranks-capstone').textContent();
    assert.match(reaperRules, /225/);
    assert.match(reaperRules, /75/);
    assert.doesNotMatch(reaperRules, /300|150/, 'Reaper explanation uses the current server limits');
    s.comp.ladder.ranks = ranks;
    s.auth.rank = {rank: 9, rank_name: ranks.top, division: null, rr: 402, top: true};
    await push();
    assert.equal(await page.locator('.profile-ranks [aria-current="step"] .rank-badge use').getAttribute('href'), '#rk-25');
    assert.equal(await page.locator('.profile-ranks-capstone .profile-ranks-division').count(), 0);
    assert.match(await page.locator('.profile-ranks-capstone').textContent(), /No divisions/);
    s.auth.rank = null; s.auth.placing = true; s.auth.placements_left = 3;
    await push();
    assert.equal(await page.locator('.profile-ranks [aria-current="step"]').count(), 0, 'Placements never earn a rank marker');
    assert.match(await page.locator('.profile-ranks-current').textContent(), /Unranked/);
    assert.match(await page.locator('.profile-ranks-current').textContent(), /3/);
    await page.getByRole('button', {name: 'Back to profile', exact: true}).click();
    await page.getByRole('button', {name: 'View all ranks', exact: true}).click();
    await page.waitForSelector('.profile-ranks');
    s.auth.placing = false;
    await push();
    assert.match(await page.locator('.profile-ranks-current').textContent(), /Unranked/);
    assert.equal(await page.locator('.profile-ranks [aria-current="step"]').count(), 0);
    s.auth.rank = {rank: 5, rank_name: ranks.names[4], division: 2, rr: 42, top: false};
    const renamed = {...ranks, names: ranks.names.map((name, index) => index === 4 ? 'Renamed fifth rank' : name), top: 'Renamed capstone'};
    s.comp.ladder.ranks = renamed;
    s.auth.rank.rank_name = renamed.names[4];
    await push();
    assert.deepEqual(await page.locator('.profile-ranks-card h2').allTextContents(), [...renamed.names, renamed.top], 'Rank names come from the server');
    assert.equal(await page.locator('.profile-ranks [aria-current="step"] .rank-badge use').getAttribute('href'), '#rk-14', 'Renaming preserves the icon and marker');
    s.comp.ladder.ranks = ranks; s.auth.rank.rank_name = ranks.names[4];
    await page.locator('#friends-toggle').click();
    assert(await page.locator('#friends-panel').isVisible());
    for (const [lang, strings] of Object.entries(fixture.languages)) {
      s.strings = strings.shared; s.profile.strings = strings.profile;
      for (const [width, height] of [[800, 560], [1200, 760]]) {
        await page.setViewportSize({width, height}); await push();
        assert(await page.locator('.profile-ranks').evaluate(n => {
          const r = n.getBoundingClientRect();
          return n.scrollWidth <= n.clientWidth + 1 && [...n.querySelectorAll('.profile-ranks-card,.profile-ranks-head,.profile-ranks-current')].every(e => { const b = e.getBoundingClientRect(); return b.left >= r.left - 1 && b.right <= r.right + 1; });
        }), lang + ' at ' + width + ' fits horizontally');
        assert(!/profile_ranks_|profile_back_/.test(await page.locator('.profile-ranks').textContent()), lang + ' has translated labels');
        assert(!/\{\w+\}/.test(await page.locator('.profile-ranks').textContent()), lang + ' resolves rule parameters');
      }
    }
    s.strings = fixture.languages.en.shared; s.profile.strings = fixture.languages.en.profile;
    await push();
    if (process.env.HUB_RANK_SCREENSHOT) {
      await page.locator('#fr-minimize').click();
      await page.locator('.profile-ranks-capstone').scrollIntoViewIfNeeded();
      await page.screenshot({path: process.env.HUB_RANK_SCREENSHOT});
    }
    s.comp.ladder = null; await push();
    assert.equal(await page.locator('.profile-ranks-card').count(), 0, 'No fabricated ladder when server data is absent');
    assert.match(await page.locator('.profile-ranks').textContent(), /Rank details are not available yet/);
    s.auth.signed_in = false; s.profile.signed_in = false; await push();
    assert.equal(await page.locator('.profile-ranks').count(), 0, 'Signing out closes the guide');
    s.auth.signed_in = true; s.profile.signed_in = true; await push();
    assert.equal(await page.locator('.profile').count(), 1, 'Signing back in starts at profile');
    assert.deepEqual(errors, []);
    assert.equal(calls.some(c => c[0].includes('rank')), false, 'Guide needs no backend actions');
    console.log('Profile/Competitive rank guide: mouse/keyboard entry, all 25 badges, unranked/placement guards, back focus, redraw/scroll persistence, no level watermark, 7 languages at 2 sizes.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });

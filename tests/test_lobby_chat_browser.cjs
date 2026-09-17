// UTF-8. Start tools/dev/lobby_preview.py, then: node tests/test_lobby_chat_browser.cjs [URL]
// Requires Playwright; CHROME_PATH may override the local Chromium executable.
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  try {
    const page = await browser.newPage({ viewport: { width: 1200, height: 760 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const url = process.argv[2] || 'http://localhost:8813';
    await page.request.post(url + '/verb/set_view', {data:['competitive'],headers:{origin:new URL(url).origin}});
    await page.goto(url);
    await page.waitForSelector('.chat-row input');
    assert(!/Match chat\. Only your team|Everyone in the match can read this\./.test(await page.locator('.chat-log').textContent()), 'No introductory system lines in match chat');
    assert.equal(await page.locator('.chat-row input').count(), 1);
    assert.equal(await page.locator('.tm-mute').count(), 9);
    const input = page.locator('.chat-row input');
    await input.fill('private draft');
    await page.waitForTimeout(1200); // a real clock push rebuilds the DOM
    assert.equal(await input.inputValue(), 'private draft');
    await page.locator('[data-channel="all"]').click();
    await page.waitForTimeout(650);
    assert.equal(await input.inputValue(), '', 'a team draft must not move into all chat');
    await input.fill('public line');
    await input.press('Enter');
    await page.waitForFunction(() => document.querySelector('.chat-log').textContent.includes('public line'));
    assert.match(await page.locator('.chat-line').last().innerText(), /\(All\).*public line/);
    await page.locator('[data-channel="team"]').click();
    await page.waitForTimeout(650);
    assert.equal(await input.inputValue(), 'private draft');
    await input.press('Enter');
    await page.waitForFunction(() => document.querySelector('.chat-log').textContent.includes('private draft'));
    assert.match(await page.locator('.chat-line').last().innerText(), /\(Team\).*private draft/);
    await page.locator('.tm-mute').first().click();
    await page.waitForFunction(() => document.querySelector('.tm-mute').getAttribute('aria-pressed') === 'true');
    await page.waitForTimeout(1200);
    assert.equal(await page.locator('.tm-mute').first().getAttribute('aria-pressed'), 'true');
    await page.locator('.tm-mute').first().click();
    await page.waitForFunction(() => document.querySelector('.tm-mute').getAttribute('aria-pressed') === 'false');

    // Freeze the service fixture so layout/scroll tests can advance the real renderer explicitly.
    const snapshot = await (await page.request.get(url + '/state')).json();
    await page.route('**/state', route => route.fulfill({ json: snapshot }));
    const languages = JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON || 'python',
      ['-c', 'import json; from hub.i18n import STRINGS; print(json.dumps(STRINGS))'], { encoding: 'utf8' }));
    for (const [language, strings] of Object.entries(languages)) {
      snapshot.strings = strings;
      for (const [width, height] of [[800, 560], [1200, 560], [1140, 700], [1200, 760], [1920, 1080]]) {
        await page.setViewportSize({ width, height });
        await page.evaluate(s => window.__hub.onState(s), snapshot);
        await page.waitForTimeout(80);
        const geometry = await page.evaluate(() => {
          const pane = document.querySelector('.pane-lobby').getBoundingClientRect();
          const mapList = document.querySelector('.veto-list');
          const maps = [...document.querySelectorAll('.veto-map')].map(n => {
            const r = n.getBoundingClientRect();
            return r.top >= pane.top && r.bottom <= pane.bottom && r.right <= innerWidth + 1;
          });
          const log = document.querySelector('.chat-log').getBoundingClientRect();
          const row = document.querySelector('.chat-row').getBoundingClientRect();
          const veto = mapList.getBoundingClientRect();
          return { maps, noMapScroll: mapList.scrollHeight <= mapList.clientHeight + 1,
            noOverlap: veto.bottom <= log.top, composerVisible: row.bottom <= innerHeight + 1,
            inputWidth: document.querySelector('.chat-row input').getBoundingClientRect().width };
        });
        assert.equal(geometry.maps.length, 7);
        assert(geometry.maps.every(Boolean) && geometry.noMapScroll && geometry.noOverlap &&
          geometry.composerVisible && geometry.inputWidth > 80, JSON.stringify({ language, width, height, geometry }));
      }
    }
    snapshot.strings = languages.en;
    snapshot.comp.lobby.messages = Array.from({ length: 80 }, (_, i) => ({
      channel: i % 2 ? 'all' : 'team', name: 'Alpha', text: 'message ' + i }));
    await page.setViewportSize({ width: 1140, height: 700 });
    await page.evaluate(s => window.__hub.onState(s), snapshot);
    await page.waitForTimeout(100);
    await page.evaluate(() => {
      const log = document.querySelector('.chat-log');
      log.scrollTop = 30;
      log.dispatchEvent(new Event('scroll'));
      const roster = document.querySelector('.lobby-top');
      roster.scrollTop = 10;
      roster.dispatchEvent(new Event('scroll'));
    });
    snapshot.comp.lobby.stage_seconds--;
    await page.evaluate(s => window.__hub.onState(s), snapshot);
    await page.waitForTimeout(100);
    assert.equal(await page.locator('.chat-log').evaluate(n => n.scrollTop), 30);
    assert.equal(await page.locator('.lobby-top').evaluate(n => n.scrollTop), 10);
    await page.evaluate(() => {
      const log = document.querySelector('.chat-log');
      log.scrollTop = log.scrollHeight;
      log.dispatchEvent(new Event('scroll'));
    });
    snapshot.comp.lobby.messages.push({ channel: 'all', name: 'Alpha', text: 'newest' });
    const immediateBottomGap = await page.evaluate(s => {
      window.__hub.onState(s);
      const log = document.querySelector('.chat-log');
      return log.scrollHeight - log.clientHeight - log.scrollTop;
    }, snapshot);
    assert(immediateBottomGap < 2, 'chat must be at the bottom before render yields, gap=' + immediateBottomGap);
    await page.waitForTimeout(100);
    assert(await page.locator('.chat-log').evaluate(n => n.scrollHeight - n.clientHeight - n.scrollTop < 2));
    await input.fill('old match draft');
    snapshot.comp.lobby.chat_epoch++;
    snapshot.comp.lobby.messages = [];
    snapshot.comp.lobby.chat_channel = 'team';
    await page.evaluate(s => window.__hub.onState(s), snapshot);
    await page.waitForTimeout(100);
    assert.equal(await input.inputValue(), '', 'a new lobby must not inherit the previous draft');
    const banRequest = page.waitForRequest(r => r.url().endsWith('/verb/ban_map'));
    const map = page.locator('button.veto-map').first();
    const mapName = await map.locator('.vm-name').innerText();
    await map.click();
    assert.deepEqual((await banRequest).postDataJSON(), [mapName]);
    assert.deepEqual(errors, []);
    console.log('PASS: one composer, private drafts and new-match reset, channel labels, mute persistence, 35 localized viewport layouts, scroll retention, follow-newest, map click.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

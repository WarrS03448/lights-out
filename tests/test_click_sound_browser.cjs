// Start tools/dev/lobby_preview.py 8836, then run with Playwright available in NODE_PATH.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  try {
    const page = await browser.newPage({ viewport: { width: 1200, height: 760 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.playCalls = [];
      const play = HTMLMediaElement.prototype.play;
      HTMLMediaElement.prototype.play = function () {
        window.playCalls.push({ src: this.src, volume: this.volume });
        return play.call(this);
      };
    });
    const url = process.argv[2] || 'http://localhost:8836';
    await page.request.post(url + '/verb/settings_set_click_volume', { data: [35], headers: {origin: new URL(url).origin} });
    await page.request.post(url + '/verb/settings_set_click_enabled', { data: [true], headers: {origin: new URL(url).origin} });
    await page.goto(url);
    await page.locator('[data-view="settings"]').click();
    await page.locator('#settings-click-volume').waitFor();
    // Use the real bridge so persisted preferences survive the next snapshot redraw.
    await page.evaluate(() => { window.playCalls = []; });
    const test = page.locator('#settings-click-volume').locator('..').getByRole('button');
    await test.click();
    assert.equal(await page.evaluate(() => playCalls.length), 1, 'Preview plays exactly once');
    assert.equal(await page.evaluate(() => playCalls[0].volume), 0.35);
    const media = await page.evaluate(async () => {
      const cue = new Audio('audio/ui-click.wav');
      await new Promise((resolve, reject) => { cue.onloadedmetadata = resolve; cue.onerror = reject; });
      await cue.play();
      return { duration: cue.duration, error: cue.error };
    });
    assert(media.duration > 0.06 && media.duration < 0.08 && !media.error);
    await page.evaluate(() => { window.playCalls = []; });
    await page.locator('#settings-click-enabled').uncheck();
    await test.click();
    assert.equal(await page.evaluate(() => playCalls.length), 0, 'Mute is immediate');
    await page.locator('#settings-click-enabled').check();
    await page.locator('#settings-click-volume').fill('64');
    await page.locator('#settings-click-volume').dispatchEvent('change');
    await test.click();
    assert.equal(await page.evaluate(() => playCalls.at(-1).volume), 0.64);
    await page.evaluate(() => {
      window.playCalls = [];
      const button = document.createElement('button');
      button.innerHTML = '<span>Nested control</span>';
      document.body.appendChild(button);
      button.firstChild.click(); button.firstChild.click();
      button.disabled = true; button.firstChild.click();
      button.remove();
      document.querySelector('h1').click();
    });
    assert.equal(await page.evaluate(() => playCalls.length), 2, 'Nested rapid clicks play; disabled/background do not');
    const quietClicks = await page.evaluate(() => {
      window.playCalls = [];
      const container = document.createElement('div');
      container.innerHTML = '<input type="search"><input type="text"><input type="checkbox"><input type="range"><textarea></textarea><select><option>Language</option></select><details><summary>Details</summary></details><span role="switch">Toggle</span><span role="checkbox">Check</span><div role="button"><input type="search"></div>';
      document.body.appendChild(container);
      container.querySelectorAll('input, textarea, select, summary, [role="switch"], [role="checkbox"]').forEach(el => el.click());
      container.remove();
      return playCalls.length;
    });
    assert.equal(quietClicks, 0, 'Form controls stay quiet, including search nested in a clickable container');
    await page.locator('#settings-click-volume').fill('0');
    await page.locator('#settings-click-volume').dispatchEvent('change');
    await page.evaluate(() => { window.playCalls = []; });
    await test.click();
    assert.equal(await page.evaluate(() => playCalls.length), 0, 'Zero volume is silent');
    await page.reload();
    await page.locator('#settings-click-volume').waitFor();
    assert.equal(await page.locator('#settings-click-volume').inputValue(), '0', 'Preference survives reload');
    const snapshot = await (await page.request.get(url + '/state')).json();
    await page.route('**/state', route => route.fulfill({ json: snapshot }));
    await page.evaluate(s => window.__hub.onState(s), snapshot);
    await page.setViewportSize({ width: 800, height: 560 });
    await page.locator('#settings-click-volume').scrollIntoViewIfNeeded();
    assert(await page.locator('#settings-click-volume').isVisible());
    await page.screenshot({ path: 'build/click-sound-settings.png' });
    assert.deepEqual(errors, []);
    console.log('Click sound browser checks passed: real WAV playback, preview, mute, volume, rapid/disabled/background clicks, narrow settings.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

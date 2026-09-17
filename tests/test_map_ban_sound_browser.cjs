// Run against tools/dev/lobby_preview.py with Playwright on NODE_PATH.
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.plays = [];
      const play = HTMLMediaElement.prototype.play;
      HTMLMediaElement.prototype.play = function () {
        const entry = { src: this.src, volume: this.volume, started: false };
        plays.push(entry);
        return play.call(this).then(() => { entry.started = true; });
      };
    });
    const url = process.argv[2] || 'http://localhost:8847';
    let snapshot = await (await page.request.get(url + '/state')).json();
    assert(Array.isArray(snapshot.comp.map_bans), 'Confirmed bans must be available across match phases');
    assert(snapshot.comp.match_id, 'Ban tracking needs a match identity');
    snapshot.view = 'competitive';
    snapshot.settings.click_sound = { enabled: true, volume: 35 };
    snapshot.comp.lobby.my_turn = false;
    await page.route('**/state', route => route.fulfill({ json: snapshot }));
    await page.route('**/verb/ban_map', route => route.fulfill({ json: {} }));
    await page.goto(url);
    await page.locator('.veto-map').first().waitFor();
    await page.locator('body').click({ position: { x: 1, y: 1 } }); // User activation for real media playback.
    assert.equal(await page.evaluate(() => plays.length), 0, 'Existing bans are not replayed on load');
    const initial = snapshot.comp.map_bans.slice();
    const addBan = (map, team = 2) => {
      snapshot.comp.map_bans.push({ map, team });
      if (snapshot.comp.lobby) snapshot.comp.lobby.veto.bans = snapshot.comp.map_bans.slice();
    };
    const deliver = () => page.evaluate(s => window.__hub.onState(s), snapshot);
    addBan('Airsoft');
    // Exercise the actual /state polling path, without clicking any map.
    await page.locator('.veto-map.banned').filter({ hasText: 'Airsoft' }).waitFor();
    assert.equal(await page.evaluate(() => plays.length), 1, 'Another captain banning a map plays the cue');
    await page.waitForFunction(() => plays[0].started);
    assert.match(await page.evaluate(() => plays[0].src), /\/audio\/map-ban\.mp3$/);
    assert.equal(await page.evaluate(() => plays[0].volume), 0.35);
    snapshot.comp.lobby.stage_seconds--;
    await deliver();
    snapshot.view = 'settings';
    await deliver();
    assert.equal(await page.evaluate(() => plays.length), 1, 'Timer and navigation redraws do not repeat bans');
    addBan('BombHouse', 1);
    await deliver();
    assert.equal(await page.evaluate(() => plays.length), 2, 'A ban is audible on another screen');
    snapshot.view = 'competitive';
    snapshot.comp.lobby.my_turn = true;
    await deliver();
    assert.equal(await page.evaluate(() => plays.length), 3, 'The captain is notified when their turn begins');
    assert.match(await page.evaluate(() => plays.at(-1).src), /\/audio\/map-ban-turn\.mp3$/);
    await page.waitForFunction(() => plays.at(-1).started);
    snapshot.comp.lobby.stage_seconds--;
    await deliver();
    assert.equal(await page.evaluate(() => plays.length), 3, 'The turn clock does not repeat the notification');
    await page.locator('button.veto-map').filter({ hasText: 'Hospital' }).click();
    assert.equal(await page.evaluate(() => plays.length), 3, 'Clicking does not play before the ban is confirmed');
    addBan('Hospital', 1);
    snapshot.comp.lobby.my_turn = false;
    await deliver();
    assert.equal(await page.evaluate(() => plays.length), 4, 'The local captain hears one confirmed cue');
    assert.match(await page.evaluate(() => plays.at(-1).src), /\/audio\/map-ban\.mp3$/);
    snapshot.settings.click_sound.enabled = false;
    addBan('Paintball');
    snapshot.comp.lobby.my_turn = true;
    await deliver();
    snapshot.settings.click_sound.enabled = true;
    await deliver();
    assert.equal(await page.evaluate(() => plays.length), 4, 'Muted bans and turns do not replay when unmuted');
    addBan('Pool', 1);
    await deliver();
    assert.equal(await page.evaluate(() => plays.length), 5, 'A captain controlling both teams hears each new turn once');
    assert.match(await page.evaluate(() => plays.at(-1).src), /\/audio\/map-ban-turn\.mp3$/);
    snapshot.settings.click_sound.volume = 64;
    addBan('Russian');
    snapshot.comp.phase = 'connecting';
    snapshot.comp.connect = { map: 'Russian', host: {}, teams: [], roster: [] };
    delete snapshot.comp.lobby;
    await deliver();
    assert.equal(await page.evaluate(() => plays.length), 6, 'The final ban survives the transition out of the lobby');
    assert.equal(await page.evaluate(() => plays.at(-1).volume), 0.64);
    snapshot.comp.match_id = 'next-match';
    snapshot.comp.map_bans = initial;
    await deliver();
    assert.equal(await page.evaluate(() => plays.length), 6, 'A recovered match does not replay old bans');
    snapshot.comp.phase = 'lobby';
    snapshot.comp.lobby = { stage: 'veto', my_turn: true, ban_turn: 1, veto: { pool: [], bans: initial } };
    snapshot.view = 'settings';
    await deliver();
    assert.equal(await page.evaluate(() => plays.length), 7, 'A recovered captain is notified of their pending turn on any screen');
    assert.match(await page.evaluate(() => plays.at(-1).src), /\/audio\/map-ban-turn\.mp3$/);
    snapshot.comp.lobby.my_turn = false;
    await deliver();
    snapshot.comp.lobby.my_turn = true;
    await deliver();
    assert.equal(await page.evaluate(() => plays.length), 7, 'Replayed state cannot repeat the same turn');
    assert.deepEqual(errors, []);
    console.log('PASS: confirmed bans and captain turn notifications, real MP3 playback, polling, redraws, navigation, mute, volume, and recovery');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

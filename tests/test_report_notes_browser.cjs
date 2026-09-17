// UTF-8. Run with Playwright on NODE_PATH: node tests/test_report_notes_browser.cjs
const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '../hub/webui/static');
(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    const page = await browser.newPage({viewport: {width: 800, height: 560}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setContent('<html><body></body></html>');
    await page.addScriptTag({path: path.join(root, 'ui.js')});
    await page.addStyleTag({path: path.join(root, 'ui.css')});
    await page.addStyleTag({path: path.join(root, 'screens/competitive.css')});
    await page.evaluate(() => {
      window.calls = [];
      HubUI.registerScreen = () => {};
      HubUI.registerOverlay = (name, module) => { if (name === 'report') window.overlay = module; };
      window.comp = {report_target: '76561198000999001', report_name: 'Friend', report_match: 'saved', report_seq: 1,
        report_reasons: ['cheating','text_abuse','voice_abuse','afk','griefing','team_killing','smurfing','other']};
      window.draw = () => {
        document.querySelectorAll('.ui-overlay').forEach(n => n.remove());
        overlay.render({comp}, {ui: HubUI, call: (...args) => calls.push(args), t: key => key});
      };
    });
    await page.addScriptTag({path: path.join(root, 'screens/competitive.js')});
    await page.evaluate(() => draw());
    await page.getByRole('button', {name: 'comp_reason_other', exact: true}).click();
    assert.deepEqual(await page.evaluate(() => calls), []);
    const submit = page.getByRole('button', {name: 'comp_report_submit', exact: true});
    assert.equal(await submit.isDisabled(), true);
    await page.locator('#report-details').fill('My custom explanation <script>');
    await page.evaluate(() => draw());
    assert.equal(await page.locator('#report-details').inputValue(), 'My custom explanation <script>');
    await submit.click();
    assert.deepEqual(await page.evaluate(() => calls), [['report','76561198000999001','other','saved','My custom explanation <script>']]);
    await page.evaluate(() => { comp.report_error = 'Please try again'; draw(); });
    assert.equal(await page.locator('#report-details').inputValue(), 'My custom explanation <script>');
    await page.evaluate(() => { comp.report_seq++; draw(); });
    assert.equal(await page.locator('.rep-custom').isVisible(), false);
    await page.getByRole('button', {name: 'comp_reason_cheating', exact: true}).click();
    assert.deepEqual((await page.evaluate(() => calls)).at(-1), ['report','76561198000999001','cheating','saved']);
    assert.deepEqual(errors, []);
    console.log('PASS: Other requires text, survives redraw/failure, submits text, resets for next report; standard reasons send immediately');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

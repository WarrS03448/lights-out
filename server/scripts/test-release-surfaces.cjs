// Run: node --test scripts/test-release-surfaces.cjs (isolated, no external services).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
for (const key of ['UPSTASH_REDIS_REST_URL', 'UPSTASH_REDIS_REST_TOKEN', 'STEAM_API_KEY']) delete process.env[key];
const { createServer } = require('../server.cjs');

async function downloadServer(t, hub) {
  const read = fs.readFileSync;
  const cataloguePath = path.resolve(__dirname, '../public/catalogue.json');
  t.mock.method(fs, 'readFileSync', (filename, ...args) =>
    typeof filename === 'string' && path.resolve(filename) === cataloguePath
      ? JSON.stringify({hub}) : read(filename, ...args));
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  });
  return `http://127.0.0.1:${server.address().port}`;
}

test('website installer download stays on the reached hostname despite the catalogue app host', async t => {
  const base = await downloadServer(t, {download_url:'https://play.lightsoutranked.com/hub/Fixture.exe?download=1'});
  for (const method of ['GET', 'HEAD']) {
    const response = await fetch(base+'/hub/download', {method, redirect:'manual', headers:{
      host:'lightsout.up.railway.app', 'x-lightsout-request-host':'lightsoutranked.com',
      'x-forwarded-host':'attacker.example', 'x-forwarded-proto':'http',
    }});
    assert.equal(response.status, 302);
    assert.equal(response.headers.get('location'), '/hub/Fixture.exe?download=1');
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.equal((await response.arrayBuffer()).byteLength, 0);
  }
});

test('website still prefers a published zip on the same host', async t => {
  const base = await downloadServer(t, {download_url:'https://play.lightsoutranked.com/hub/Fixture.exe',
    zip_url:'https://lightsout.up.railway.app/hub/Fixture.zip'});
  const response = await fetch(base+'/hub/download', {redirect:'manual'});
  assert.equal(response.status,302);
  assert.equal(response.headers.get('location'),'/hub/Fixture.zip');
});

test('external and private capability download URLs retain their destination', async t => {
  for (const target of ['https://downloads.example.test/hub/Fixture.exe',
    'https://private.example.test/private/capability/hub/Fixture.exe',
    'https://play.lightsoutranked.com/private/capability/hub/Fixture.exe']) {
    await t.test(target, async sub => {
      const base = await downloadServer(sub, {download_url:target});
      const response = await fetch(base+'/hub/download', {redirect:'manual'});
      assert.equal(response.status,302);
      assert.equal(response.headers.get('location'),target);
    });
  }
});

test('retired diagnostics and permit routes are unavailable anonymously', async () => {
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    for (const route of ['/probe', '/probe.html', '/api/probe/log', '/api/probe/clear',
      '/api/probe/join', '/api/probe/team', '/api/probe/wait']) {
      for (const method of ['GET', 'POST', 'HEAD']) {
        assert.equal((await fetch(base + route, { method })).status, 404, `${method} ${route}`);
      }
    }
    assert.equal((await fetch(base + '/api/probe', { method: 'POST', body: '{}' })).status, 200);
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test('strict versions are the release default and diagnostics exclude private headers', () => {
  const { GATE_MODE } = require('../live.cjs');
  assert.equal(GATE_MODE, 'strict');
  const { describeHeaders } = require('../server.cjs')._internals;
  assert.deepEqual(describeHeaders({authorization:'Bearer private', cookie:'session=private',
    'x-forwarded-for':'203.0.113.1', 'x-real-ip':'203.0.113.2', 'content-type':'application/json'}),
  {authorization:'[redacted]', 'content-type':'application/json'});
});

test('test identities cannot authenticate in production or an unspecified environment', async () => {
  const { create } = require('../auth.cjs');
  const auth = create({ upstashCmd: async () => null, prefix: 'release-test:' });
  process.env.HUB_TEST_TOKENS = 'fixture=76561198000000001';
  try {
    for (const env of ['production', '']) {
      process.env.NODE_ENV = env;
      assert.equal(await auth.whoami('fixture'), null);
    }
    process.env.NODE_ENV = 'test';
    assert.equal((await auth.whoami('fixture')).steam_id, '76561198000000001');
  } finally { delete process.env.HUB_TEST_TOKENS; delete process.env.NODE_ENV; }
});

// ------------------------------------------------------------------ the native Linux beta
//
// A fixture of the top-level `linux` entry publish.py writes (tools/release, pub-release-tools).
// server/public/catalogue.json itself never carries one until publish.py adds it at release.

const LINUX_SHA = '0f'.repeat(32);
const LINUX_SOURCE_SHA = 'a1'.repeat(32);
const LINUX = {
  version: '3.0.3', kind: 'linux-native-zip', arch: 'x86_64',
  download_url: 'https://play.lightsoutranked.com/hub/LightsOut-Linux-Native-3.0.3-x86_64.zip',
  size: 35065462, sha256: LINUX_SHA,
  source_url: 'https://lightsoutranked.com/hub/LightsOut-Linux-Native-3.0.3-source.zip',
  source_size: 55416894, source_sha256: LINUX_SOURCE_SHA,
  page_url: 'https://lightsoutranked.com/faq#faq-linux', required: false,
  update: { key_id: 'fixture-key', manifest: 'e30=', signature: 'AAAA' },
};
const HUB = { version: '3.0.3', download_url: 'https://play.lightsoutranked.com/hub/LightsOut-Setup-3.0.3.exe',
  zip_url: 'https://play.lightsoutranked.com/hub/LightsOut-Setup-3.0.3.zip' };
const MODES = [{ id: 'BB5', title: 'Bodybomb 5v5', version: '1.0.30', rulesets: { en: 'First team to 7 wins.' } }];
const LINUX_PAGES = ['/', '/about', '/how', '/ranked', '/tournament', '/account'];
const LINUX_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36';
const AGENTS = [
  LINUX_UA,
  'Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0',
  'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36',
  'Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
];

/** Serves `text` as server/public/catalogue.json for the rest of test `t`. */
function mockCatalogue(t, text) {
  const read = fs.readFileSync;
  const cataloguePath = path.resolve(__dirname, '../public/catalogue.json');
  t.mock.method(fs, 'readFileSync', (filename, ...args) =>
    typeof filename === 'string' && path.resolve(filename) === cataloguePath ? text : read(filename, ...args));
}

async function catalogueServer(t, catalogue) {
  mockCatalogue(t, typeof catalogue === 'string' ? catalogue : JSON.stringify(catalogue));
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  });
  return `http://127.0.0.1:${server.address().port}`;
}

test('both Linux routes send a same-origin 302 on GET and HEAD, whatever the Host headers say', async t => {
  const base = await catalogueServer(t, { hub: HUB, gamemodes: MODES, linux: LINUX });
  const expected = {
    '/hub/download/linux': '/hub/LightsOut-Linux-Native-3.0.3-x86_64.zip',
    '/hub/download/linux-source': '/hub/LightsOut-Linux-Native-3.0.3-source.zip',
  };
  for (const [route, location] of Object.entries(expected)) {
    for (const method of ['GET', 'HEAD']) {
      const response = await fetch(base + route, { method, redirect: 'manual', headers: {
        host: 'lightsout.up.railway.app', 'x-lightsout-request-host': 'lightsoutranked.com',
        'x-forwarded-host': 'attacker.example', 'x-forwarded-proto': 'http', 'user-agent': LINUX_UA,
      } });
      assert.equal(response.status, 302, `${method} ${route}`);
      assert.equal(response.headers.get('location'), location, `${method} ${route}`);
      assert.equal(response.headers.get('cache-control'), 'no-store');
      assert.equal((await response.arrayBuffer()).byteLength, 0);
    }
  }
});

test('Linux routes rewrite every owned origin, and keep other https hosts and /hub/ paths', async t => {
  const cases = [
    ['https://lightsout.up.railway.app/hub/L.zip?v=1#x', '/hub/L.zip?v=1#x'],
    ['https://www.lightsoutranked.com/hub/L.zip', '/hub/L.zip'],
    ['https://github.com/WarrS03448/lights-out/releases/download/linux-v3.0.3/L.zip',
      'https://github.com/WarrS03448/lights-out/releases/download/linux-v3.0.3/L.zip'],
    ['https://play.lightsoutranked.com/private/capability/hub/L.zip',
      'https://play.lightsoutranked.com/private/capability/hub/L.zip'],
    ['/hub/L-source.zip', '/hub/L-source.zip'],
  ];
  for (const [url, location] of cases) {
    await t.test(url, async sub => {
      const base = await catalogueServer(sub, { hub: HUB, linux: { ...LINUX, download_url: url, source_url: url } });
      for (const route of ['/hub/download/linux', '/hub/download/linux-source']) {
        const response = await fetch(base + route, { redirect: 'manual' });
        assert.equal(response.status, 302, route);
        assert.equal(response.headers.get('location'), location, route);
      }
    });
  }
});

test('Linux routes are a JSON 404, never a 500, when the entry or its URL is absent or unusable', async t => {
  const bad = ['', 'http://play.lightsoutranked.com/hub/L.zip', 'javascript:alert(1)', 'data:text/plain,x',
    '//attacker.example/hub/L.zip', '/other/L.zip', '/hub/../api/health', 'hub/L.zip',
    'https://user:pass@play.lightsoutranked.com/hub/L.zip', 'https://play.lightsoutranked.com/hub/L.zip\r\nx-bad: 1',
    'https://play.lightsoutranked.com/hub/L .zip', '/\\attacker.example/hub/L.zip', 'https://', 42, {}, ['/hub/L.zip'], null];
  const catalogues = [
    ['no linux entry', { hub: HUB, gamemodes: MODES }],
    ['linux is null', { hub: HUB, linux: null }],
    ['linux is an array', { hub: HUB, linux: [LINUX] }],
    ['linux is a string', { hub: HUB, linux: LINUX.download_url }],
    ['no URLs', { hub: HUB, linux: { ...LINUX, download_url: undefined, source_url: undefined } }],
    ['unreadable catalogue', '{not json'],
    ...bad.map(url => [`URL ${JSON.stringify(url)}`, { hub: HUB, linux: { ...LINUX, download_url: url, source_url: url } }]),
  ];
  for (const [name, catalogue] of catalogues) {
    await t.test(name, async sub => {
      const base = await catalogueServer(sub, catalogue);
      for (const route of ['/hub/download/linux', '/hub/download/linux-source']) {
        const response = await fetch(base + route, { redirect: 'manual' });
        assert.equal(response.status, 404, `${name} ${route}`);
        assert.match(response.headers.get('content-type') || '', /application\/json/);
        assert.equal(response.headers.get('location'), null);
        assert.equal(typeof (await response.json()).error, 'string');
        const head = await fetch(base + route, { method: 'HEAD', redirect: 'manual' });
        assert.equal(head.status, 404);
        assert.equal(await head.text(), '');
      }
    });
  }
});

test('the Linux URL check refuses without throwing, and bounds the length', () => {
  const { linuxDownloadLocation } = require('../server.cjs')._internals;
  for (const value of [undefined, null, 42, {}, [], ['/hub/L.zip'], true, { toString: () => '/hub/L.zip' }]) {
    assert.equal(linuxDownloadLocation(value), null, String(value));
  }
  const long = 'https://play.lightsoutranked.com/hub/' + 'L'.repeat(2048) + '.zip';
  assert.equal(linuxDownloadLocation(long), null);
  assert.equal(linuxDownloadLocation(long.slice(0, 2048)), long.slice('https://play.lightsoutranked.com'.length, 2048));
  assert.equal(linuxDownloadLocation('https://Play.LightsOutRanked.com/hub/L.zip'), '/hub/L.zip');
  assert.equal(linuxDownloadLocation('https://downloads.example.test/hub/lä.zip'), 'https://downloads.example.test/hub/l%C3%A4.zip');
});

test('only GET and HEAD reach the Linux routes, and a trailing path is not one of them', async t => {
  const base = await catalogueServer(t, { hub: HUB, linux: LINUX });
  for (const method of ['POST', 'PUT', 'DELETE']) {
    const response = await fetch(base + '/hub/download/linux', { method, redirect: 'manual' });
    assert.notEqual(response.status, 302, method);
    assert.equal(response.headers.get('location'), null, method);
  }
  for (const route of ['/hub/download/linux/', '/hub/download/linux-source/x', '/hub/download/LINUX']) {
    const response = await fetch(base + route, { redirect: 'manual' });
    assert.ok(response.status === 400 || response.status === 404, `${route} got ${response.status}`);
  }
});

test('/hub/download is unchanged by a Linux entry and never reads the User-Agent', async t => {
  const base = await catalogueServer(t, { hub: HUB, gamemodes: MODES, linux: LINUX });
  for (const agent of [undefined, ...AGENTS]) {
    for (const method of ['GET', 'HEAD']) {
      const response = await fetch(base + '/hub/download', { method, redirect: 'manual',
        headers: agent ? { 'user-agent': agent } : {} });
      assert.equal(response.status, 302, `${method} ${agent}`);
      assert.equal(response.headers.get('location'), '/hub/LightsOut-Setup-3.0.3.zip', `${method} ${agent}`);
      assert.equal(response.headers.get('cache-control'), 'no-store');
    }
  }
  const { sameHostDownloadLocation } = require('../server.cjs')._internals;
  // The Windows rewrite, factored out, is the same function it always was.
  assert.equal(sameHostDownloadLocation('https://play.lightsoutranked.com/hub/F.exe?download=1'), '/hub/F.exe?download=1');
  assert.equal(sameHostDownloadLocation('https://downloads.example.test/hub/F.exe'), 'https://downloads.example.test/hub/F.exe');
  assert.equal(sameHostDownloadLocation('https://play.lightsoutranked.com/private/hub/F.exe'), 'https://play.lightsoutranked.com/private/hub/F.exe');
  assert.equal(sameHostDownloadLocation('/hub/F.exe'), '/hub/F.exe');
  assert.equal(sameHostDownloadLocation('https://u:p@play.lightsoutranked.com/hub/F.exe'), 'https://u:p@play.lightsoutranked.com/hub/F.exe');
});

test('catalogue.linux is served untouched, with or without a rules override', async t => {
  const text = JSON.stringify({ hub: HUB, gamemodes: MODES, linux: LINUX }, null, 2);
  const base = await catalogueServer(t, text);
  const plain = await fetch(base + '/catalogue.json');
  assert.equal(plain.status, 200);
  assert.equal(await plain.text(), text, 'served byte for byte');
  process.env.COMP_GAME_RULES_OVERRIDE = '{"BB5":{"score_limit":2}}';
  try {
    const served = await (await fetch(base + '/catalogue.json')).json();
    assert.ok(served.gamemodes[0].rules_override, 'the override did apply');
    assert.deepEqual(served.linux, LINUX, 'and left the Linux entry alone');
    assert.deepEqual(served.hub, HUB);
  } finally { delete process.env.COMP_GAME_RULES_OVERRIDE; }
});

test('requiredVersions adds the Linux version and keeps hub and mode exactly as before', async t => {
  const { requiredVersions } = require('../server.cjs')._internals;
  await t.test('with a Linux entry', sub => {
    mockCatalogue(sub, JSON.stringify({ hub: HUB, gamemodes: MODES, linux: LINUX }));
    assert.deepEqual(requiredVersions('BB5'), { hub: '3.0.3', mode: '1.0.30', linux: '3.0.3' });
  });
  await t.test('without one', sub => {
    mockCatalogue(sub, JSON.stringify({ hub: HUB, gamemodes: [{ ...MODES[0], id: 'CTF' }] }));
    assert.deepEqual(requiredVersions('CTF'), { hub: '3.0.3', mode: '1.0.30', linux: '' });
  });
});

test('pages offer both downloads, fill the Linux facts and keep their disclosures', async t => {
  const base = await catalogueServer(t, { hub: HUB, gamemodes: MODES, linux: LINUX });
  for (const page of LINUX_PAGES) {
    const body = await (await fetch(base + page)).text();
    assert.match(body, /<a class="btn-primary" href="\/hub\/download"/, `${page} keeps the Windows download`);
    assert.match(body, /<a class="btn-ghost" href="\/hub\/download\/linux"[^>]*>Download for Linux \(beta\)<\/a>/, `${page} offers Linux`);
    assert.ok(body.indexOf('href="/hub/download"') < body.indexOf('href="/hub/download/linux"'), `${page}: Windows first`);
    assert.match(body, /href="\/hub\/download\/linux-source"/, `${page} links the source`);
    assert.match(body, /Linux beta<\/span> 3\.0\.3 · x86_64|Linux beta 3\.0\.3 for x86_64 PCs/, `${page} names the version and arch`);
    assert.match(body, /<script src="\/assets\/platform\.js" defer><\/script>/, `${page} loads platform.js`);
    assert.match(body, /[Nn]ot affiliated with(?: or endorsed by)? Reissad Studio/, `${page} keeps non-affiliation`);
    assert.doesNotMatch(body, /\{\{[A-Z_]+\}\}/, `${page} leaves no placeholder`);
    assert.doesNotMatch(body, /linux-offer/, `${page}: the offer markers never reach the browser`);
    assert.doesNotMatch(body, /Download Lights Out|data-copy="download">Download the app<\/a>\s*<a class="btn-ghost"/, page);
  }
  // The account just created signs in to the Windows app; the Linux beta is Steam sign-in only.
  assert.match(await (await fetch(base + '/account')).text(),
    /<span data-copy="linuxAccount">Your new account works in the Windows app\. The Linux beta signs in with Steam only\.<\/span>/);
  const how = await (await fetch(base + '/how')).text();
  assert.match(how, /<section class="section" id="linux">[\s\S]*<h2>On Linux<\/h2>/);
  // Each page's footer keeps its disclosure, word for word.
  for (const page of ['/', '/about', '/how', '/ranked', '/faq', '/privacy', '/cookies', '/notices']) {
    const body = await (await fetch(base + page)).text();
    assert.ok(body.includes('<p>Not affiliated with Reissad Studio. This tool does not modify game files; it adds a removable pak. Version 3.0.3.</p>'), page);
  }
  assert.match(await (await fetch(base + '/tournament')).text(),
    /not affiliated with or endorsed by Reissad Studio\. It supports agreed community matches; its purpose is not to cheat, and nothing it installs is a cheat\./);
  assert.match(await (await fetch(base + '/account')).text(),
    /data-copy="disclosure">Lights Out provides community gamemodes and ranked matches, not cheats\. Not affiliated with or endorsed by Reissad Studio\./);
  // The SmartScreen help belongs to the Windows button alone.
  const home = await (await fetch(base + '/')).text();
  assert.match(home, /<a class="btn-primary" href="\/hub\/download" aria-describedby="download-help">Download for Windows<\/a>/);
  assert.match(home, /<a class="btn-ghost" href="\/hub\/download\/linux" aria-describedby="linux-download-note">/);
  assert.equal(home.split('Windows protected your PC').length - 1, 1);
  const faq = await (await fetch(base + '/faq')).text();
  assert.match(faq, /<details id="faq-smartscreen">/);
  assert.match(faq, /<details id="faq-linux">/);
  for (const hash of [LINUX_SHA, LINUX_SOURCE_SHA]) assert.ok(faq.includes(`<code>${hash}</code>`), 'FAQ shows both SHA-256s');
  const notices = await (await fetch(base + '/notices')).text();
  assert.ok(notices.includes(`<code>${LINUX_SOURCE_SHA}</code>`));
  assert.match(notices, /<section aria-labelledby="legal-linux">[\s\S]*href="\/hub\/download\/linux-source"/);
});

// The FAQ's general answers, by question, from a rendered /faq.
function faqAnswers(faq) {
  const answers = {};
  for (const [, question, answer] of faq.matchAll(/<summary>([^<]+)<\/summary>\s*<div class="answer">([\s\S]*?)<\/div>\s*<\/details>/g)) {
    answers[question] = answer;
  }
  return answers;
}

test('the general FAQ answers scope what is Windows-only, and send Linux readers to the Linux answer', async t => {
  const SCOPED = { 'How do I remove it completely?': /<p>Uninstall inside the app takes the gamemode file back out of Bodycam\. On Windows, Lights Out itself then uninstalls like any Windows program;/,
    'How do updates to the app or the gamemodes work?': /next to the mode\. On Windows, a newer app version installs itself in the background, so there is no need to come back here\.<\/p>/ };
  const LINUX_LINE = '<p>On Linux, see <a href="#faq-linux">Is there a Linux version?</a></p>';
  for (const [name, linux] of [['with a Linux release', LINUX], ['without one', undefined]]) {
    await t.test(name, async sub => {
      const base = await catalogueServer(sub, { hub: HUB, gamemodes: MODES, ...(linux ? { linux } : {}) });
      const answers = faqAnswers(await (await fetch(base + '/faq')).text());
      assert.ok(Object.keys(answers).length >= 10, 'the FAQ parsed');
      for (const [question, scoped] of Object.entries(SCOPED)) {
        assert.match(answers[question] || '', scoped, question);
        assert.equal((answers[question] || '').includes(LINUX_LINE), Boolean(linux), `${question}: the Linux pointer only with a release`);
      }
      // Nothing else describes the Windows app's install, uninstall or updates without saying so.
      for (const [question, answer] of Object.entries(answers)) {
        if (/%LOCALAPPDATA%|like any Windows program|installs itself in the background/.test(answer)) {
          assert.match(answer, /On Windows, /, question);
        }
      }
      assert.equal(Object.hasOwn(answers, 'Is there a Linux version?'), Boolean(linux));
    });
  }
});

test('the FAQ carries the bundle README platform wording word for word', async t => {
  const base = await catalogueServer(t, { hub: HUB, linux: LINUX });
  const faq = await (await fetch(base + '/faq')).text();
  // tools/linux/native/build_bundle.py README (English), joined onto one line.
  assert.ok(faq.includes('Needs: a 64-bit PC (x86_64) with glibc 2.35 or newer, an X11 or Wayland desktop, '
    + 'and Bodycam installed through the regular Steam app. The Flatpak and Snap versions of Steam, ARM PCs, '
    + 'musl-based systems (such as Alpine) and NixOS are not supported yet. Links open with xdg-open, sound '
    + 'needs PulseAudio or PipeWire, and the window needs working OpenGL graphics drivers.'));
  for (const fact of ['Install and Open Lights Out', 'Proton', 'Steam only', 'each time you open it',
    'Close Bodycam yourself', 'update strip', 'newer download', 'open Settings in Lights Out and choose Uninstall',
    '~/.local/share/lights-out-linux-beta', '~/.local/state/lights-out-linux-beta']) {
    assert.ok(faq.includes(fact), `FAQ Linux answer covers "${fact}"`);
  }
  // Manual Bodycam closure is the accepted limitation: nothing may promise the opposite.
  assert.doesNotMatch(faq, /closes (?:Bodycam|the game|it) (?:for you|automatically)|automatically closes/i);
});

test('without a complete, usable Linux entry the pages offer no Linux download at all', async t => {
  const { linuxPageFacts, renderLinuxOffer } = require('../server.cjs')._internals;
  assert.deepEqual(linuxPageFacts({}), { version: '', sha256: '', source_sha256: '', offered: false });
  assert.deepEqual(linuxPageFacts({ linux: { ...LINUX, version: '3.0.3.1', sha256: 'AB'.repeat(32) } }),
    { version: '3.0.3.1', sha256: 'ab'.repeat(32), source_sha256: LINUX_SOURCE_SHA, offered: true });
  assert.equal(renderLinuxOffer('a<!-- linux-offer -->b$&$1<!-- /linux-offer -->c', true), 'ab$&$1c');
  assert.equal(renderLinuxOffer('a<!-- linux-offer -->b<!-- /linux-offer -->c<!-- linux-offer -->d<!-- /linux-offer -->', false), 'ac');
  const unusable = [
    ['no entry', undefined], ['null', null], ['a string', 'x'], ['an array', [LINUX]],
    ['hostile values', { ...LINUX, version: '<script>alert(1)</script>', sha256: '"><b>', source_sha256: 'zz'.repeat(32) }],
    ['bad shapes', { ...LINUX, version: '3.0', sha256: 'ab'.repeat(31), source_sha256: 'ab'.repeat(33) }],
    ['wrong types', { ...LINUX, version: 3.1, sha256: 1, source_sha256: {} }],
    ['no version', { ...LINUX, version: undefined }],
    ['no ZIP SHA-256', { ...LINUX, sha256: '' }],
    ['no source SHA-256', { ...LINUX, source_sha256: undefined }],
    ['no download URL', { ...LINUX, download_url: undefined }],
    ['an unusable download URL', { ...LINUX, download_url: 'http://play.lightsoutranked.com/hub/L.zip' }],
    ['no source URL', { ...LINUX, source_url: '' }],
    ['an unusable source URL', { ...LINUX, source_url: 'javascript:alert(1)' }],
  ];
  for (const [name, linux] of unusable) {
    await t.test(name, async sub => {
      const base = await catalogueServer(sub, { hub: HUB, gamemodes: MODES, ...(linux === undefined ? {} : { linux }) });
      for (const page of [...LINUX_PAGES, '/faq', '/notices', '/privacy', '/cookies']) {
        const response = await fetch(base + page);
        assert.equal(response.status, 200, page);
        const body = await response.text();
        assert.doesNotMatch(body, /\{\{[A-Z_]+\}\}|undefined|null<|<script>alert|"><b>|linux-offer/, page);
        assert.doesNotMatch(body, /\/hub\/download\/linux|faq-linux|linux-download-note|Download for Linux|Linux beta \d|<code><\/code>|id="linux"/,
          `${page} says nothing that needs a Linux release`);
      }
      // The source offer goes with the binary; the privacy and storage wording stays.
      assert.doesNotMatch(await (await fetch(base + '/notices')).text(), /legal-linux/);
      assert.match(await (await fetch(base + '/cookies')).text(), /<h2 id="legal-linux">Linux beta app storage<\/h2>/);
      for (const page of LINUX_PAGES) {
        const body = await (await fetch(base + page)).text();
        assert.match(body, /<a class="btn-primary" href="\/hub\/download"/, `${page} keeps the Windows download`);
      }
    });
  }
});

test('every Linux offer in the page sources sits inside one closed linux-offer block', () => {
  const dir = path.resolve(__dirname, '../public');
  for (const name of fs.readdirSync(dir).filter(file => file.endsWith('.html'))) {
    const text = fs.readFileSync(path.join(dir, name), 'utf8');
    const markers = [...text.matchAll(/<!--\s*(\/?)\s*linux-offer\s*-->/g)];
    markers.forEach((marker, i) => {
      assert.equal(marker[0], i % 2 ? '<!-- /linux-offer -->' : '<!-- linux-offer -->', `${name}: marker ${i} opens, closes and is spelt as the server expects`);
    });
    assert.equal(markers.length % 2, 0, `${name}: every block is closed`);
    const outside = text.replace(/<!-- linux-offer -->[\s\S]*?<!-- \/linux-offer -->/g, '');
    assert.doesNotMatch(outside, /\/hub\/download\/linux|faq-linux|linux-download-note|\{\{LINUX_/, `${name}: a Linux offer outside a block`);
  }
});

test('new Linux text on the site uses no em dash', () => {
  const dir = path.resolve(__dirname, '../public');
  for (const name of ['index.html', 'about.html', 'how.html', 'ranked.html', 'tournament.html', 'account.html',
    'faq.html', 'privacy.html', 'cookies.html', 'notices.html', 'assets/platform.js']) {
    for (const line of fs.readFileSync(path.join(dir, name), 'utf8').split(/\r?\n/)) {
      if (/Linux|linux/.test(line)) assert.doesNotMatch(line, /\u2014/, `${name}: ${line.slice(0, 80)}`);
    }
  }
  const strings = {};
  require('node:vm').runInNewContext(fs.readFileSync(path.join(dir, 'assets/account-i18n.js'), 'utf8'), { window: strings });
  for (const [culture, copy] of Object.entries(strings.LightsOutAccountStrings)) {
    for (const key of ['downloadWindows', 'downloadLinux', 'linuxBeta', 'linuxAccount', 'source']) {
      assert.ok(copy[key] && !copy[key].includes('\u2014'), `${culture}.${key}`);
    }
    assert.match(copy.downloadLinux, /Linux/, culture);
    // Where the new account works, and that the Linux beta takes Steam sign-in instead.
    assert.match(copy.linuxAccount, /Windows[\s\S]*Linux[\s\S]*Steam/, culture);
  }
});

// platform.js, run against a small stand-in for the DOM it touches.
function platformPage(userAgent, userAgentData, { separated = false, fixed = false } = {}) {
  const link = (href, cls) => ({
    href, classes: new Set([cls]), parentNode: null,
    classList: { add(c) { this.owner.classes.add(c); }, remove(c) { this.owner.classes.delete(c); } },
  });
  const windows = link('/hub/download', 'btn-primary');
  const linux = link('/hub/download/linux', 'btn-ghost');
  for (const a of [windows, linux]) a.classList.owner = a;
  const match = (selector, a) => {
    const [, cls, href] = /^a\.([\w-]+)\[href="([^"]+)"\]$/.exec(selector);
    return a.classes.has(cls) && a.href === href;
  };
  const row = (children) => {
    const node = { children, querySelector: s => node.children.find(a => match(s, a)) || null,
      getAttribute: name => (name === 'data-download-order' && fixed ? 'fixed' : null),
      insertBefore(a, before) {
        node.children.splice(node.children.indexOf(a), 1);
        node.children.splice(node.children.indexOf(before), 0, a);
      } };
    for (const a of children) a.parentNode = node;
    return node;
  };
  const rows = separated ? [row([windows]), row([linux])] : [row([windows, linux])];
  const document = { querySelectorAll: s => rows.flatMap(r => r.children).filter(a => match(s, a)) };
  const script = fs.readFileSync(path.resolve(__dirname, '../public/assets/platform.js'), 'utf8');
  require('node:vm').runInNewContext(script, { navigator: { userAgent, userAgentData }, document, Promise });
  return { windows, linux, rows };
}

test('platform.js promotes Linux only on a Linux x86_64 desktop, and both buttons stay', async () => {
  const hints = (platform, architecture, bitness, fail) => ({ platform, mobile: false,
    getHighEntropyValues: async () => { if (fail) throw new Error('denied'); return { architecture, bitness }; } });
  const cases = [
    ['Firefox, Linux x86_64', AGENTS[1], undefined, true],
    ['Chrome, Linux x86_64', LINUX_UA, hints('Linux', 'x86', '64'), true],
    ['Chrome, Linux on ARM (the UA still says x86_64)', LINUX_UA, hints('Linux', 'arm', '64'), false],
    ['Chrome, 32-bit x86 Linux', LINUX_UA, hints('Linux', 'x86', '32'), false],
    ['Chrome, hints refused', LINUX_UA, hints('Linux', 'x86', '64', true), false],
    ['Chrome, mobile hint', LINUX_UA, { ...hints('Linux', 'x86', '64'), mobile: true }, false],
    ['Firefox, Linux aarch64', 'Mozilla/5.0 (X11; Linux aarch64; rv:140.0) Gecko/20100101 Firefox/140.0', undefined, false],
    ['Android', AGENTS[2], hints('Android', '', ''), false],
    ['Android naming x86_64', 'Mozilla/5.0 (X11; Linux x86_64; Android 14) Firefox/140.0', undefined, false],
    ['ChromeOS', AGENTS[3], hints('Chrome OS', 'x86', '64'), false],
    // Chrome's "Desktop site" on an x86_64 Android device (an emulator, an x86 tablet, ARC) sends the
    // plain desktop Linux User-Agent; only the platform hint tells it apart. Likewise ChromeOS.
    ['Chrome "Desktop site", x86_64 Android', LINUX_UA, hints('Android', 'x86', '64'), false],
    ['Chrome, ChromeOS behind a plain Linux User-Agent', LINUX_UA, hints('Chrome OS', 'x86', '64'), false],
    ['Windows', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36',
      hints('Windows', 'x86', '64'), false],
    ['no User-Agent', undefined, undefined, false],
  ];
  for (const [name, agent, data, promoted] of cases) {
    const { windows, linux, rows } = platformPage(agent, data);
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.deepEqual(rows[0].children.map(a => a.href), promoted ? ['/hub/download/linux', '/hub/download'] : ['/hub/download', '/hub/download/linux'], name);
    assert.deepEqual([...linux.classes], [promoted ? 'btn-primary' : 'btn-ghost'], name);
    assert.deepEqual([...windows.classes], [promoted ? 'btn-ghost' : 'btn-primary'], name);
  }
  const apart = platformPage(AGENTS[1], undefined, { separated: true });
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual([...apart.linux.classes], ['btn-ghost'], 'only a Linux button beside a Windows one is promoted');
  const fixed = platformPage(AGENTS[1], undefined, { fixed: true });
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(fixed.rows[0].children.map(a => a.href), ['/hub/download', '/hub/download/linux'], 'a fixed row keeps its order');
  assert.deepEqual([...fixed.linux.classes], ['btn-ghost'], 'and its classes');
});

// ---------------------------------------- platform.js on the real pages, laid out by site.css's order rules
//
// A small tree builder for the site's own well-formed pages: elements, attributes, parent and
// children, and the few DOM calls platform.js makes. Comments are not elements; script and style
// bodies are skipped.
const VOID_TAGS = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr']);

function compoundMatches(compound, el) {
  const parts = /^([a-z][a-z0-9]*|\*)?((?:\.[\w-]+|#[\w-]+|\[[\w-]+(?:="[^"]*")?\])*)$/i.exec(compound);
  if (!parts) throw new Error(`unsupported selector part: ${compound}`);
  if (parts[1] && parts[1] !== '*' && parts[1].toLowerCase() !== el.tag) return false;
  for (const [, kind, name, value] of parts[2].matchAll(/([.#\[])([\w-]+)(?:="([^"]*)")?\]?/g)) {
    const ok = kind === '.' ? el.classes.has(name)
      : kind === '#' ? el.attrs.id === name
        : value === undefined ? Object.hasOwn(el.attrs, name) : el.attrs[name] === value;
    if (!ok) return false;
  }
  return true;
}

function selectorMatches(selector, el) {
  if (/[>+~:()]/.test(selector.replace(/\[[^\]]*\]/g, ''))) throw new Error(`unsupported selector: ${selector}`);
  const parts = selector.trim().split(/\s+/);
  if (!compoundMatches(parts.pop(), el)) return false;
  let node = el.parentNode;
  for (let i = parts.length - 1; i >= 0; i--) {
    while (node && !compoundMatches(parts[i], node)) node = node.parentNode;
    if (!node) return false;
    node = node.parentNode;
  }
  return true;
}

function parsePage(html) {
  const all = [];
  const contains = (ancestor, node) => {
    for (let n = node.parentNode; n; n = n.parentNode) if (n === ancestor) return true;
    return false;
  };
  const element = (tag, attrs, parentNode) => {
    const el = { tag, attrs, parentNode, children: [], classes: new Set((attrs.class || '').split(/\s+/).filter(Boolean)),
      getAttribute: name => (Object.hasOwn(attrs, name) ? attrs[name] : null),
      querySelector: selector => all.find(node => contains(el, node) && selectorMatches(selector, node)) || null,
      insertBefore(node, before) {
        el.children.splice(el.children.indexOf(node), 1);
        el.children.splice(el.children.indexOf(before), 0, node);
        return node;
      } };
    el.classList = { add: name => el.classes.add(name), remove: name => el.classes.delete(name) };
    if (parentNode) parentNode.children.push(el);
    return el;
  };
  const root = element('#document', {}, null);
  const stack = [root];
  const lower = html.toLowerCase();
  const tags = /<!--[\s\S]*?-->|<(\/?)([a-zA-Z][a-zA-Z0-9]*)((?:[^>"']|"[^"]*"|'[^']*')*)>/g;
  for (let m; (m = tags.exec(html));) {
    if (!m[2]) continue;
    const tag = m[2].toLowerCase();
    if (m[1]) {
      const at = stack.map(el => el.tag).lastIndexOf(tag);
      if (at > 0) stack.length = at;
      continue;
    }
    const attrs = {};
    for (const [, name, value] of m[3].matchAll(/([^\s=/"']+)(?:\s*=\s*"([^"]*)")?/g)) attrs[name.toLowerCase()] = value ?? '';
    const el = element(tag, attrs, stack[stack.length - 1]);
    all.push(el);
    if (VOID_TAGS.has(tag) || /\/\s*$/.test(m[3])) continue;
    stack.push(el);
    if (tag === 'script' || tag === 'style') {
      const end = lower.indexOf(`</${tag}`, tags.lastIndex);
      tags.lastIndex = end < 0 ? html.length : end;
    }
  }
  return { querySelectorAll: selector => all.filter(el => selectorMatches(selector, el)) };
}

/** Every site.css rule that sets `order`, one per selector, with the @media blocks it sits in. */
function cssOrderRules() {
  const css = fs.readFileSync(path.resolve(__dirname, '../public/assets/site.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = [];
  (function walk(text, media) {
    for (let i = 0, open; (open = text.indexOf('{', i)) >= 0;) {
      let prelude = text.slice(i, open);
      prelude = prelude.slice(prelude.lastIndexOf(';') + 1).trim();
      let depth = 1;
      let end = open + 1;
      for (; depth && end < text.length; end++) depth += text[end] === '{' ? 1 : text[end] === '}' ? -1 : 0;
      const body = text.slice(open + 1, end - 1);
      if (prelude.startsWith('@media')) walk(body, [...media, prelude.slice('@media'.length).trim()]);
      else if (!prelude.startsWith('@')) {
        const order = /(?:^|[;\s])order\s*:\s*(-?\d+)/.exec(body);
        if (order) for (const selector of prelude.split(',')) rules.push({ selector: selector.trim(), order: Number(order[1]), media });
      }
      i = end;
    }
  })(css, []);
  return rules;
}

function mediaApplies(query, width, height) {
  return query.split(',').some(alternative => alternative.trim().split(/\s+and\s+/).every(feature => {
    const m = /^\((min|max)-(width|height)\s*:\s*(\d+)px\)$/.exec(feature.trim());
    if (!m) return false;
    const value = m[2] === 'width' ? width : height;
    return m[1] === 'min' ? value >= Number(m[3]) : value <= Number(m[3]);
  }));
}

function specificity(selector) {
  const counts = [0, 0, 0];
  for (const part of selector.trim().split(/\s+/)) {
    const bare = part.replace(/\[[^\]]*\]/g, '[]');
    counts[0] += (bare.match(/#[\w-]+/g) || []).length;
    counts[1] += (bare.match(/\.[\w-]+|\[\]/g) || []).length;
    if (/^[a-z]/i.test(bare)) counts[2]++;
  }
  return counts;
}

/** A flex row's children in the order they are laid out at this viewport. */
function visualOrder(row, rules, width, height = 900) {
  const applicable = rules.filter(rule => rule.media.every(query => mediaApplies(query, width, height)));
  const compare = (a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2];
  const orderOf = el => {
    let best = null;            // the most specific matching rule; a later one wins a tie
    for (const rule of applicable) {
      if (!selectorMatches(rule.selector, el)) continue;
      const weight = specificity(rule.selector);
      if (!best || compare(weight, best.weight) >= 0) best = { weight, order: rule.order };
    }
    return best ? best.order : 0;
  };
  return row.children.map((el, index) => ({ el, index, order: orderOf(el) }))
    .sort((a, b) => a.order - b.order || a.index - b.index).map(item => item.el);
}

async function runPlatformScript(document, userAgent, userAgentData) {
  const script = fs.readFileSync(path.resolve(__dirname, '../public/assets/platform.js'), 'utf8');
  require('node:vm').runInNewContext(script, { navigator: { userAgent, userAgentData }, document, Promise });
  await new Promise(resolve => setTimeout(resolve, 0));
}

test('at every width the promoted download leads and the SmartScreen help follows the Windows button', async t => {
  const base = await catalogueServer(t, { hub: HUB, gamemodes: MODES, linux: LINUX });
  const rules = cssOrderRules();
  assert.ok(rules.some(rule => rule.media.some(query => /max-width:\s*620px/.test(query))), 'site.css still orders the stacked row');
  const label = el => el.attrs.id || el.attrs.href || el.tag;
  for (const page of LINUX_PAGES) {
    const html = await (await fetch(base + page)).text();
    for (const linuxDesktop of [false, true]) {
      const document = parsePage(html);
      if (linuxDesktop) await runPlatformScript(document, AGENTS[1], undefined);
      const [linux, ...others] = document.querySelectorAll('a[href="/hub/download/linux"]');
      assert.ok(linux && others.length === 0, `${page}: one Linux download`);
      const row = linux.parentNode;
      const windows = row.children.find(el => el.attrs.href === '/hub/download');
      assert.ok(windows, `${page}: the two downloads share a row`);
      const fixed = row.getAttribute('data-download-order') === 'fixed';
      assert.equal(fixed, page === '/account', `${page}: only the account page keeps Windows first on Linux`);
      const lead = linuxDesktop && !fixed ? linux : windows;
      const help = row.children.find(el => el.attrs.id === 'download-help');
      assert.equal(Boolean(help), page === '/', `${page}: the SmartScreen help is on the home page`);
      for (const width of [320, 390, 620, 621, 1059, 1060, 1280]) {
        const order = visualOrder(row, rules, width);
        const name = `${page} at ${width}px ${linuxDesktop ? 'on a Linux desktop' : 'elsewhere'}: ${order.map(label).join(', ')}`;
        assert.equal(order.find(el => el === linux || el === windows), lead, name);
        assert.ok(lead.classes.has('btn-primary') && !(lead === windows ? linux : windows).classes.has('btn-primary'), name);
        if (!help) continue;
        // Never ahead of the Windows button; and at 620px and below, where site.css stacks the row
        // one full-width item per line, directly under it rather than under the Linux button.
        assert.ok(order.indexOf(help) > order.indexOf(windows), name);
        if (width <= 620) assert.equal(order.indexOf(help), order.indexOf(windows) + 1, name);
      }
    }
  }
});

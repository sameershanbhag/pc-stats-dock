// Browser-side tests for dashboard/index.html and dashboard/admin.html, run under jsdom.
//   NODE_PATH=<dir with jsdom> node tests/test_pages.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { JSDOM } = require('jsdom');
const here = path.dirname(fileURLToPath(import.meta.url));
const dash = readFileSync(path.join(here, '..', 'dashboard', 'index.html'), 'utf8');
const admin = readFileSync(path.join(here, '..', 'dashboard', 'admin.html'), 'utf8');

let failures = 0, passes = 0;
const check = (cond, msg) => { if (cond) passes++; else { failures++; console.log('  FAIL:', msg); } };
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function boot(html, { fetchImpl, width = 1540, height = 720 } = {}) {
  return new JSDOM(html, {
    runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://127.0.0.1:4400/',
    beforeParse(w) {
      Object.defineProperty(w, 'innerWidth', { value: width, configurable: true });
      Object.defineProperty(w, 'innerHeight', { value: height, configurable: true });
      w.HTMLCanvasElement.prototype.getContext = () => new Proxy({}, { get: (t, k) => k === 'canvas' ? null : () => {}, set: () => true }); // canvas stub
      w.fetch = fetchImpl;
    },
  });
}
const json = (obj, ok = true, status = 200) => Promise.resolve({ ok, status, json: async () => obj });
const STATS = { pc_name: 'Test Mac', demo: false, sensors: 'macmon', lhm_ok: true, mic_muted: true, cfg_version: 1, caps: { accessibility: true, volume: 'system', mic: true },
  cpu: { name: 'Apple M4 Pro', load: 33.3, temp: 61, clock: 3200, power: 12.5 },
  gpu: { name: 'Apple M4 Pro GPU', load: 91, temp: 84, hotspot: null, vram_used_gb: null, vram_total_gb: null, fan_rpm: 1000, fan_pct: null, power: 8.2, clock: 1200 },
  ram: { used_gb: 16.4, total_gb: 64, load: 25.6, swap_used_gb: 0.5 }, sys_power: 19.4,
  storage: [{ name: 'Macintosh HD', temp: null, used_pct: 69.7, used_gb: 344, total_gb: 494 }],
  net: { name: 'en1', down_mbps: 12.3, up_mbps: 1.1 }, fans: [{ name: 'fan0', rpm: 1000 }] };
const CONFIG = { pc_name: 'Test Mac', platform: 'mac', cfg_version: 1, buttons: [
  { id: 'space-prev', label: 'Space', sub: 'previous', glyph: '◀', type: 'hotkey', keys: ['ctrl', 'left'] },
  { id: 'mic', label: 'Mic', sub: 'mute', glyph: '●', type: 'mic' }, { type: 'empty' },
  { id: 'app1', label: 'Safari', sub: 'open', glyph: '◎', type: 'app', app: 'Safari' } ] };

async function testDashboardLive() {
  console.log('dashboard · live agent');
  const posted = [];
  let statsVersion = 1; let configCalls = 0;
  const fetchImpl = (url, opts = {}) => {
    if (url.startsWith('/api/config')) { configCalls++; return json({ ...CONFIG, cfg_version: statsVersion, buttons: statsVersion === 1 ? CONFIG.buttons : CONFIG.buttons.slice(0, 2) }); }
    if (url.startsWith('/api/stats')) return json({ ...STATS, cfg_version: statsVersion });
    if (url.startsWith('/api/action/')) { posted.push(url); return json({ ok: true, message: 'sent ctrl+left' }); }
    if (url.startsWith('/api/admin/open')) { posted.push(url); return json({ ok: true, message: 'opened' }); }
    return json({}, false, 404);
  };
  const dom = boot(dash, { fetchImpl }); const d = dom.window.document;
  await sleep(150);
  check(d.getElementById('pcName').textContent === 'Test Mac', 'pc name rendered');
  check(d.getElementById('dot').className === 'dot live', 'live dot');
  check(d.querySelectorAll('#keys .key').length === 4, `4 key tiles (got ${d.querySelectorAll('#keys .key').length})`);
  check(d.querySelectorAll('#keys .key.blank').length === 1, 'blank slot rendered inert');
  check(d.querySelector('#r-cpu-load .v').textContent === '33', 'cpu load ring text');
  check(d.querySelector('#r-gpu-temp .arc').getAttribute('stroke') === 'var(--warn)', 'gpu temp 84 shows warning colour');
  check(d.querySelector('#r-cpu-temp .arc').getAttribute('stroke') === 'var(--teal)', 'cpu temp 61 normal colour');
  check(d.getElementById('t-ram').textContent.startsWith('16.4'), 'memory tile');
  check(d.getElementById('k-vram').textContent === 'System power' && d.getElementById('t-vram').textContent.includes('19.4'), 'VRAM tile becomes system power on a Mac');
  check(d.getElementById('k-fan').textContent === 'Fan' && d.getElementById('t-gpu-fan').textContent.startsWith('1000'), 'fan tile');
  check(d.getElementById('k-hot').textContent === 'GPU clock' && d.getElementById('t-hotspot').textContent.startsWith('1200'), 'hot spot tile becomes GPU clock');
  check(d.getElementById('k-drive').textContent === 'Disk used' && d.getElementById('t-nvme').textContent.startsWith('70'), 'drive tile shows disk used');
  check(d.getElementById('t-net').textContent.includes('12.3'), 'network tile');
  check(d.getElementById('t-cpu-clock').textContent.startsWith('3.20'), 'cpu clock GHz');
  check(d.querySelector('.key[data-type="mic"]').classList.contains('on'), 'mic tile red while muted');
  const first = d.querySelector('#keys .key[data-id="space-prev"]');
  first.dispatchEvent(new dom.window.Event('pointerdown', { bubbles: true }));
  await sleep(30);
  check(posted[0] === '/api/action/space-prev', 'tap posts the action');
  check(d.getElementById('toast').textContent === 'sent ctrl+left', 'toast shows agent message');
  d.getElementById('adminBtn').dispatchEvent(new dom.window.Event('click', { bubbles: true }));
  await sleep(30);
  check(posted.includes('/api/admin/open'), 'gear asks the agent to open the admin page');
  // config hot reload when the version changes
  statsVersion = 2; await sleep(700);
  check(configCalls >= 2 && d.querySelectorAll('#keys .key').length === 2, `buttons reloaded after version bump (calls=${configCalls}, keys=${d.querySelectorAll('#keys .key').length})`);
  const stage = d.getElementById('stage');
  check(/scale\(1\)/.test(stage.style.transform), `stage scaled 1:1 at 1540x720 (${stage.style.transform})`);
  dom.window.close();
}

async function testDashboardCaps() {
  console.log('dashboard · capability notes');
  let caps = { accessibility: false, volume: 'none', mic: false };
  const fetchImpl = (url) => url.startsWith('/api/config') ? json({ ...CONFIG, buttons: [...CONFIG.buttons, { id: 'vol', label: 'Volume', sub: 'up', glyph: '+', type: 'volume', dir: 'up' }] })
    : url.startsWith('/api/stats') ? json({ ...STATS, caps }) : json({}, false, 404);
  const dom = boot(dash, { fetchImpl }); const d = dom.window.document;
  await sleep(150);
  check(d.getElementById('banner').classList.contains('show') && d.getElementById('banner').textContent.includes('Accessibility'), 'permission banner shown');
  check(d.querySelector('.key[data-type="hotkey"] .sb').textContent === 'needs permission', 'hotkey tile notes the permission');
  check(d.querySelector('.key[data-type="volume"] .sb').textContent === 'no volume on this output', 'volume tile notes the output');
  check(d.querySelector('.key[data-type="mic"] .sb').textContent === 'no microphone', 'mic tile notes missing mic');
  caps = { accessibility: true, volume: 'ddc', mic: true }; await sleep(600);
  check(!d.getElementById('banner').classList.contains('show'), 'banner hides once granted');
  check(d.querySelector('.key[data-type="hotkey"] .sb').textContent === 'previous', 'original sub text restored');
  check(d.querySelector('.key[data-type="volume"] .sb').textContent === 'monitor volume (DDC)', 'volume tile shows DDC');
  dom.window.close();
}

async function testDashboardAi() {
  console.log('dashboard · AI chats feed');
  const now = Date.now() / 1000; const focused = [];
  let feedsResp = { right_side: 'feeds3', feeds: [
    { id: 'feed-1', title: 'AI chats', source: 'ai', status: 'ok', updated: now, items: [
      { id: 'abcdef0123', who: 'Claude Code', where: 'proj · Terminal', text: 'Finished — tests pass', ts: now - 30, link: 'focus:abcdef0123', state: 'done', seen: false },
      { id: 'abcdef0124', who: 'Codex', where: 'billing · VS Code', text: 'Needs your attention', ts: now - 300, link: 'focus:abcdef0124', state: 'needs_input', seen: true } ] },
    { id: 'feed-2', title: 'Slack', source: 'slack', status: 'needs_setup', items: [] },
    { id: 'feed-3', title: 'Teams', source: 'notifications', status: 'needs_fda', items: [] } ] };
  const fetchImpl = (url, opts = {}) => {
    if (url.startsWith('/api/config')) return json({ ...CONFIG, right_side: 'feeds3', feeds: [{ id: 'feed-1', title: 'AI chats', source: 'ai' }, { id: 'feed-2', title: 'Slack', source: 'slack' }, { id: 'feed-3', title: 'Teams', source: 'notifications' }] });
    if (url.startsWith('/api/stats')) return json(STATS);
    if (/^\/api\/events\/[0-9a-f]{10}\/focus$/.test(url)) { focused.push(url); feedsResp.feeds[0].items[0].seen = true; return json({ ok: true, message: 'jumped to Terminal' }); }
    if (url.startsWith('/api/feeds')) return json(feedsResp);
    return json({}, false, 404);
  };
  const dom = boot(dash, { fetchImpl }); const d = dom.window.document;
  await sleep(200);
  check(d.getElementById('right').className === 'right feeds3' && d.querySelectorAll('.feed').length === 3 && !d.querySelector('#keys'), 'three feed panels and no stray button grid');
  const ai = d.querySelector('.feed[data-id="feed-1"]');
  check(ai.querySelector('.st').textContent === '1 new' && ai.querySelector('.st').classList.contains('new'), 'header counts unseen');
  const items = ai.querySelectorAll('.msg.ai');
  check(items.length === 2 && items[0].classList.contains('unseen') && !items[1].classList.contains('unseen'), 'unseen styling');
  check(items[1].classList.contains('needs') && items[1].querySelector('.state').textContent.includes('needs you'), 'needs-input styling and label');
  items[0].dispatchEvent(new dom.window.Event('pointerdown', { bubbles: true })); await sleep(60);
  check(focused[0] === '/api/events/abcdef0123/focus', 'tap posts focus for that event');
  check(d.getElementById('toast').textContent === 'jumped to Terminal', 'toast shows the jump result');
  await sleep(60);
  check(ai.querySelector('.st').textContent === 'all seen', 'list refreshed after the jump');
  dom.window.close();
}

async function testDashboardThreeFeeds() {
  console.log('dashboard · three-feeds layout');
  const fetchImpl = (url) => url.startsWith('/api/config') ? json({ ...CONFIG, right_side: 'three-feeds', feeds: [{ id: 'feed-1', title: 'AI', source: 'ai' }, { id: 'feed-2', title: 'Slack', source: 'slack' }, { id: 'feed-3', title: 'Teams', source: 'notifications' }] })
    : url.startsWith('/api/stats') ? json(STATS) : url.startsWith('/api/feeds') ? json({ feeds: [] }) : json({}, false, 404);
  const dom = boot(dash, { fetchImpl }); const d = dom.window.document; await sleep(150);
  check(d.getElementById('main').className === 'three' && d.querySelectorAll('#center .feed').length === 1 && d.querySelectorAll('#right .feed').length === 2 && !d.querySelector('#keys'), 'first feed centre, two feeds right, no buttons');
  dom.window.close();
  const fetch2 = (url) => url.startsWith('/api/config') ? json({ ...CONFIG, right_side: 'buttons' }) : url.startsWith('/api/stats') ? json(STATS) : json({}, false, 404);
  const dom2 = boot(dash, { fetchImpl: fetch2 }); const d2 = dom2.window.document; await sleep(150);
  check(d2.getElementById('main').className === 'two' && d2.getElementById('center').hidden && d2.querySelectorAll('#right #keys .key').length === 4, 'wide-stats buttons layout still works');
  dom2.window.close();
}

async function testDashboardStandalone() {
  console.log('dashboard · standalone (no agent)');
  const fetchImpl = () => Promise.reject(new Error('no agent'));
  const dom = boot(dash, { fetchImpl, width: 770, height: 400 }); const d = dom.window.document;
  await sleep(150);
  check(d.querySelectorAll('.feed').length === 3 && d.querySelectorAll('.feed .msg').length >= 7, `three demo feeds with messages (${d.querySelectorAll('.feed .msg').length} msgs)`);
  check(d.getElementById('main').className === 'three' && !d.getElementById('center').hidden && d.querySelectorAll('#center .feed').length === 3 && d.querySelectorAll('#right #keys .key').length === 12, 'three columns: feeds in the middle, 12 buttons on the right');
  check(d.querySelectorAll('.msg.ai.unseen').length === 2 && d.querySelectorAll('.msg.ai.needs').length === 1, 'AI demo items carry unseen/needs styling');
  check(d.getElementById('dot').className === 'dot demo', 'demo dot');
  check(d.getElementById('pcName').textContent === 'GAMING-PC' || d.getElementById('pcName').textContent.length > 0, 'demo name');
  check(d.querySelector('#r-cpu-load .v').textContent !== '—', 'demo numbers flow');
  check(/scale\(0\.5\)/.test(d.getElementById('stage').style.transform), `stage scaled to fit (${d.getElementById('stage').style.transform})`);
  dom.window.close();
}

async function testDashboardFeeds() {
  console.log('dashboard · feeds layouts');
  const now = Date.now() / 1000;
  let feedsResp = { right_side: 'feeds', feeds: [
    { id: 'feed-1', title: 'Slack', source: 'slack', status: 'ok', updated: now, items: [{ who: 'Priya', where: '#platform', text: 'deploy is green', ts: now - 120, link: 'slack://channel?team=T&id=C' }] },
    { id: 'feed-2', title: 'Teams', source: 'notifications', status: 'needs_fda', error: '', updated: now, items: [] } ] };
  const opened = [];
  const fetchImpl = (url, opts = {}) => {
    if (url.startsWith('/api/config')) return json({ ...CONFIG, right_side: 'feeds', feeds: [{ id: 'feed-1', title: 'Slack', source: 'slack' }, { id: 'feed-2', title: 'Teams', source: 'notifications' }] });
    if (url.startsWith('/api/stats')) return json(STATS);
    if (url.startsWith('/api/feeds/open')) { opened.push(JSON.parse(opts.body).url); return json({ ok: true }); }
    if (url.startsWith('/api/feeds')) return json(feedsResp);
    return json({}, false, 404);
  };
  const dom = boot(dash, { fetchImpl }); const d = dom.window.document;
  await sleep(200);
  check(d.getElementById('right').className === 'right feeds', 'feeds layout class');
  check(!d.getElementById('keys'), 'no button grid in feeds layout');
  const feeds = d.querySelectorAll('.feed');
  check(feeds.length === 2 && feeds[0].querySelector('.t').textContent === 'Slack', 'two feed panels titled');
  const msg = feeds[0].querySelector('.msg');
  check(msg && msg.querySelector('.who').textContent === 'Priya' && msg.querySelector('.text').textContent === 'deploy is green', 'slack message rendered');
  check(feeds[0].querySelector('.st').textContent.startsWith('updated'), 'ok status shows updated time');
  check(feeds[1].querySelector('.st').textContent === 'needs Full Disk Access' && feeds[1].querySelector('.empty').textContent.includes('Full Disk Access'), 'needs_fda explained');
  msg.dispatchEvent(new dom.window.Event('pointerdown', { bubbles: true })); await sleep(20);
  check(opened[0] === 'slack://channel?team=T&id=C', 'tap opens the deep link through the agent');
  dom.window.close();
  // mixed layout: one feed + six buttons
  const fetch2 = (url) => url.startsWith('/api/config') ? json({ ...CONFIG, right_side: 'mixed', feeds: [{ id: 'feed-1', title: 'Slack', source: 'slack' }] }) : url.startsWith('/api/stats') ? json(STATS) : url.startsWith('/api/feeds') ? json(feedsResp) : json({}, false, 404);
  const dom2 = boot(dash, { fetchImpl: fetch2 }); const d2 = dom2.window.document; await sleep(200);
  check(d2.getElementById('right').className === 'right mixed' && d2.querySelectorAll('.feed').length === 1, 'mixed layout has one feed');
  check(d2.querySelectorAll('#keys .key').length === 4, `mixed layout shows up to six buttons (${d2.querySelectorAll('#keys .key').length} of 4 configured)`);
  dom2.window.close();
}

async function testAdmin() {
  console.log('admin · editing, validation, save');
  const saves = [], tests = [];
  const fetchImpl = (url, opts = {}) => {
    if (url.startsWith('/api/config')) return json(CONFIG);
    if (url === '/api/admin/config') { const b = JSON.parse(opts.body); saves.push(b); return json({ ok: true, cfg_version: 2, buttons: b.buttons }); }
    if (url === '/api/admin/feeds') { const b = JSON.parse(opts.body); saves.push(b); return json({ ok: true, cfg_version: 3, right_side: b.right_side, feeds: b.feeds.map((f, i) => ({ ...f, id: 'feed-' + (i + 1), has_token: !!f.token, token: undefined })) }); }
    if (url === '/api/action') { tests.push(JSON.parse(opts.body)); return json({ ok: true, message: 'dry run' }); }
    return json({}, false, 404);
  };
  const dom = boot(admin, { fetchImpl }); const w = dom.window, d = w.document;
  await sleep(150);
  const tiles = d.querySelectorAll('#pgrid .tile');
  check(tiles.length === 12, '12 slots');
  check(tiles[0].classList.contains('sel') && tiles[2].classList.contains('empty'), 'first selected, third empty');
  check(d.getElementById('save').disabled, 'save disabled when clean');
  check(d.getElementById('fType').value === 'hotkey' && d.getElementById('fKey').value === 'left' && d.querySelector('[data-mod="ctrl"]').checked, 'editor loads slot 1');
  check(d.getElementById('combo').textContent.includes('⌃'), 'combo shows ⌃');
  // edit label -> dirty, grid updates
  const label = d.getElementById('fLabel'); label.value = 'Left'; label.dispatchEvent(new w.Event('input', { bubbles: true }));
  check(!d.getElementById('save').disabled, 'save enabled after edit');
  check(d.querySelector('#pgrid .tile .l').textContent === 'Left', 'grid tile reflects label');
  // capture a key press: Cmd+Shift+4
  const key = d.getElementById('fKey');
  key.dispatchEvent(new w.KeyboardEvent('keydown', { key: '4', code: 'Digit4', metaKey: true, shiftKey: true, bubbles: true, cancelable: true }));
  check(key.value === '4' && d.querySelector('[data-mod="cmd"]').checked && d.querySelector('[data-mod="shift"]').checked && !d.querySelector('[data-mod="ctrl"]').checked, 'key capture sets key and modifiers');
  // switch slot 4 to app type, validate empty app name, then fill
  tiles[3].click(); await sleep(10);
  check(d.getElementById('fType').value === 'app' && d.getElementById('fApp').value === 'Safari', 'slot 4 loads app');
  const app = d.getElementById('fApp'); app.value = ''; app.dispatchEvent(new w.Event('input', { bubbles: true }));
  check(d.getElementById('msg').textContent === 'Enter an app name.', 'inline validation message');
  app.value = 'Slack'; app.dispatchEvent(new w.Event('input', { bubbles: true }));
  check(d.getElementById('msg').textContent === '', 'validation clears');
  // library into slot 3 (empty)
  tiles[2].click(); await sleep(10);
  d.getElementById('search').value = 'mission'; d.getElementById('search').dispatchEvent(new w.Event('input', { bubbles: true }));
  const put = d.querySelector('#lib .item .btn');
  check(put && put.textContent === 'Put in slot 3', `library button targets selected slot (${put && put.textContent})`);
  put.click();
  check(d.getElementById('fType').value === 'hotkey' && d.getElementById('fKey').value === 'up', 'library item loaded into editor');
  // swap with arrows
  d.getElementById('moveL').click();
  check(d.querySelectorAll('#pgrid .tile')[1].classList.contains('sel'), 'moved left to slot 2');
  // test now posts an inline action
  d.getElementById('test').click(); await sleep(20);
  check(tests.length === 1 && tests[0].type === 'hotkey' && tests[0].keys.join('+') === 'ctrl+up', 'test now posts inline action');
  // save
  d.getElementById('save').click(); await sleep(30);
  const bs = saves.find(x => x.buttons);
  check(bs && bs.buttons.length === 12, 'save posts 12 slots');
  check(bs.buttons[0].label === 'Left' && bs.buttons[0].keys.join('+') === 'cmd+shift+4', 'saved slot 1 has edited label and captured keys');
  check(bs.buttons[1].type === 'hotkey' && bs.buttons[3].app === 'Slack', 'saved layout reflects swap and app edit');
  check(d.getElementById('save').disabled, 'save disabled again after saving');
  check(saves.some(x => x.right_side !== undefined), 'save also posted the right-side settings');
  check(d.getElementById('toast').textContent.startsWith('Saved'), 'saved toast');
  // revert after another edit returns to saved state
  label.value = 'Zzz'; label.dispatchEvent(new w.Event('input', { bubbles: true }));
  d.getElementById('revert').click();
  check(d.querySelector('#pgrid .tile .l').textContent === 'Left', 'revert restores last saved');
  // clear slot
  d.getElementById('clear').click();
  check(d.querySelector('#pgrid .tile.sel').classList.contains('empty'), 'clear empties the selected slot');
  dom.window.close();
}

async function testAdminRightSide() {
  console.log('admin · right side editor');
  const saves = [];
  const fetchImpl = (url, opts = {}) => {
    if (url.startsWith('/api/config')) return json({ ...CONFIG, right_side: 'feeds', feeds: [{ id: 'feed-1', title: 'Slack', source: 'slack', channels: ['dm'], limit: 8, has_token: true }, { id: 'feed-2', title: 'Teams', source: 'notifications', apps: ['Microsoft Teams'], limit: 8 }] });
    if (url === '/api/admin/config') return json({ ok: true, buttons: JSON.parse(opts.body).buttons });
    if (url === '/api/admin/feeds') { const b = JSON.parse(opts.body); saves.push(b); return json({ ok: true, right_side: b.right_side, feeds: b.feeds }); }
    return json({}, false, 404);
  };
  const dom = boot(admin, { fetchImpl }); const w = dom.window, d = w.document;
  await sleep(150);
  check(d.querySelectorAll('.fe').length === 2, 'two feed editors for the feeds layout');
  check(d.querySelector('.fe [data-k="source"] option[value="ai"]'), 'AI chats source available');
  const first = d.querySelector('.fe');
  check(first.querySelector('[data-k="token"]').placeholder.includes('stored'), 'stored token is hinted, never shown');
  check(first.querySelector('[data-k="channels"]').value === 'dm', 'channels loaded');
  check(d.querySelectorAll('.fe')[1].querySelector('[data-app="Microsoft Teams"]').checked, 'Teams preset ticked');
  check(d.getElementById('save').disabled, 'clean at start');
  const ch = first.querySelector('[data-k="channels"]'); ch.value = 'dm, platform'; ch.dispatchEvent(new w.Event('input', { bubbles: true }));
  check(!d.getElementById('save').disabled, 'editing a feed makes it dirty');
  d.getElementById('rightSide').value = 'mixed'; d.getElementById('rightSide').dispatchEvent(new w.Event('change', { bubbles: true }));
  check(d.querySelectorAll('.fe').length === 1, 'mixed layout shows one editor');
  d.getElementById('rightSide').value = 'feeds3'; d.getElementById('rightSide').dispatchEvent(new w.Event('change', { bubbles: true }));
  check(d.querySelectorAll('.fe').length === 3 && d.querySelectorAll('.fe')[2].querySelector('[data-app="Microsoft Teams"]'), 'three editors for feeds3 (third defaults to Teams notifications)');
  d.getElementById('rightSide').value = 'three'; d.getElementById('rightSide').dispatchEvent(new w.Event('change', { bubbles: true }));
  check(d.querySelectorAll('.fe').length === 3, 'three editors for the three-column layout');
  d.getElementById('rightSide').value = 'mixed'; d.getElementById('rightSide').dispatchEvent(new w.Event('change', { bubbles: true }));
  d.getElementById('save').click(); await sleep(30);
  check(saves.length === 1 && saves[0].right_side === 'mixed' && saves[0].feeds[0].channels.join(',') === 'dm,platform' && saves[0].feeds[0].token === '', 'save posts layout, channels and an empty token (keeps stored)');
  check(d.getElementById('save').disabled, 'clean after save');
  dom.window.close();
}

async function testAdminStandalone() {
  console.log('admin · standalone');
  const dom = boot(admin, { fetchImpl: () => Promise.reject(new Error('no agent')) }); const d = dom.window.document;
  await sleep(150);
  check(d.querySelector('.note') && d.querySelector('.note').textContent.includes('Preview mode'), 'preview note shown');
  check(d.getElementById('save').disabled, 'save disabled standalone');
  check(d.querySelectorAll('#pgrid .tile:not(.empty)').length === 12, 'demo layout fills 12 slots');
  d.getElementById('test').click(); await sleep(10);
  check(d.getElementById('toast').textContent.includes('Preview only'), 'test describes instead of running');
  dom.window.close();
}

for (const t of [testDashboardLive, testDashboardCaps, testDashboardFeeds, testDashboardAi, testDashboardThreeFeeds, testDashboardStandalone, testAdmin, testAdminRightSide, testAdminStandalone]) {
  try { await t(); } catch (e) { failures++; console.log('  CRASH:', e.stack || e); }
}
console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures ? 1 : 0);

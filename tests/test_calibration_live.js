// Run the actual inline UI script with delayed HTTP replies and a deterministic clock.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../pi/calibration.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const settle = () => new Promise(resolve => setImmediate(resolve));

async function page(backlight = false) {
  let now = 0, sequence = 0;
  const timers = new Map(), calls = [], elements = {};
  const server = {firmware:backlight ? 'alia-eye-wifi-10' : 'alia-eye-wifi-7', width:240, height:320, eyes:[
    {screen:1, center_x:120, center_y:160, angle:0, top:'connector', unsaved:false},
    {screen:2, center_x:120, center_y:160, angle:0, top:'opposite', unsaved:false}
  ]};
  if (backlight) for (const eye of server.eyes) eye.brightness = 8;
  const context = Object.fromEntries(['fillRect','save','translate','rotate','drawImage','restore','setLineDash','beginPath','moveTo','lineTo','stroke','arc'].map(key => [key, () => {}]));
  function element(id) {
    let value = '';
    return {id, disabled:false, textContent:'', dataset:{}, events:{},
      get value() { return value; }, set value(next) { value = String(next); },
      get valueAsNumber() { return value === '' ? NaN : Number(value); },
      classList:{toggle() {}}, setAttribute() {}, setPointerCapture() {},
      getContext() { return context; }, getBoundingClientRect() { return {left:0, top:0, width:240, height:320}; },
      addEventListener(type, handler) { (this.events[type] ||= []).push(handler); },
      fire(type, event = {}) { if (this.disabled && type === 'click') return; for (const handler of this.events[type] || []) handler({preventDefault() {}, ...event}); },
      checkValidity() {
        const n = Number(value), rules = {x:[0,239,1], y:[0,319,1], angle:[-180,180,.1], brightness:[0,100,1]};
        const [low, high, step] = rules[id];
        return value !== '' && Number.isFinite(n) && n >= low && n <= high && Math.abs(n / step - Math.round(n / step)) < 1e-7;
      }
    };
  }
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) elements[match[1]] = element(match[1]);
  const buttons = [1, 2].map(screen => { const b = element('screen'+screen); b.dataset.screen = String(screen); return b; });
  vm.runInNewContext(script, {
    document:{getElementById(id) { assert.ok(elements[id], 'Unknown element '+id); return elements[id]; }, querySelectorAll() { return buttons; }},
    Image:class {}, Date:{now:() => now},
    setTimeout(fn, delay) { const id = ++sequence; timers.set(id, {at:now+delay, fn}); return id; },
    fetch(url, options = {}) {
      assert.equal(calls.filter(call => !call.done).length, 0, 'Only one HTTP request may be in flight');
      return new Promise(resolve => calls.push({url, data:options.body && JSON.parse(options.body), resolve, done:false}));
    }
  });
  async function reply(ok = true) {
    const call = calls.find(request => !request.done); assert.ok(call, 'Expected a pending request');
    call.done = true;
    if (ok && ['/api/calibration', '/api/backlight'].includes(call.url)) Object.assign(server.eyes[call.data.screen-1], call.data, {unsaved:true});
    if (ok && call.url === '/api/reset') Object.assign(server.eyes[call.data.screen-1], {center_x:120, center_y:160, angle:0, top:call.data.screen === 1 ? 'connector' : 'opposite', brightness:8, unsaved:true});
    if (ok && call.url === '/api/save') for (const eye of server.eyes) eye.unsaved = false;
    const result = structuredClone(ok ? server : {error:'simulated timeout; result unknown'});
    call.resolve({ok, json:async () => result}); await settle();
    return call;
  }
  async function tick(ms) {
    const end = now + ms;
    while (true) {
      const entry = [...timers].sort((a,b) => a[1].at-b[1].at)[0];
      if (!entry || entry[1].at > end) break;
      now = entry[1].at; timers.delete(entry[0]); entry[1].fn(); await settle();
    }
    now = end;
  }
  function input(id, value, type = 'input') { elements[id].value = value; elements[id].fire(type); }
  await reply(); // Initial state read.
  return {elements, buttons, server, calls, reply, tick, input, last:() => calls.at(-1)};
}

(async () => {
  assert.ok(!html.includes('id="apply"'), 'Realtime editing must not require an apply button');
  {
    const p = await page(), e = p.elements;
    assert.equal(e.brightness.disabled, true, 'Keep geometric calibration available on older firmware');
    assert.equal(e['brightness-range'].disabled, true);
    e.preview.fire('pointerdown', {pointerId:1, clientX:130, clientY:170});
    e.preview.fire('pointermove', {clientX:140, clientY:175});
    await p.tick(0);
    assert.deepEqual(p.last().data, {screen:1, center_x:140, center_y:175, angle:0, top:'connector'});
    assert.equal(e.editor.disabled, false, 'Inputs must remain usable during live HTTP');
    e.preview.fire('pointermove', {clientX:150, clientY:180});
    e.preview.fire('pointermove', {clientX:160, clientY:190});
    p.input('angle', 5.5);
    await p.tick(300);
    assert.equal(p.calls.length, 2, 'Do not accumulate drag requests while awaiting a reply');
    await p.reply();
    assert.equal(e.x.value, '160'); assert.equal(e.y.value, '190'); assert.equal(e.angle.value, '5.5');
    await p.tick(0);
    assert.deepEqual(p.last().data, {screen:1, center_x:160, center_y:190, angle:5.5, top:'connector'});
    await p.reply();
    assert.equal(p.calls.filter(c => c.url === '/api/save').length, 0, 'Dragging must never write NVS');
    assert.equal(e.save.disabled, false);
    e.save.fire('click'); assert.equal(p.last().url, '/api/save'); await p.reply();
    assert.ok(p.server.eyes.every(eye => !eye.unsaved));
  }
  {
    const p = await page(), e = p.elements;
    p.input('x', 130); await p.tick(0);
    p.input('x', 120); // Revert while the old position is still in flight.
    p.buttons[1].fire('click'); p.input('x', 125);
    await p.tick(200); await p.reply();
    assert.equal(e.x.value, '125', 'An eye 1 reply must not overwrite eye 2 editing');
    await p.tick(0);
    assert.equal(p.last().data.screen, 1); assert.equal(p.last().data.center_x, 120);
    await p.reply(); await p.tick(100);
    assert.equal(p.last().data.screen, 2); assert.equal(p.last().data.center_x, 125);
    await p.reply();
    assert.equal(p.server.eyes[0].center_x, 120); assert.equal(p.server.eyes[1].center_x, 125);
  }
  {
    const p = await page();
    p.input('x', 130); p.input('x', ''); await p.tick(0);
    assert.equal(p.calls.length, 1, 'A cleared numeric field cancels its queued value');
    p.input('x', 240); await p.tick(500); assert.equal(p.calls.length, 1);
    p.input('x', 131); await p.tick(0); assert.equal(p.last().data.center_x, 131);
    p.input('x', ''); p.buttons[1].fire('click'); p.buttons[0].fire('click');
    assert.equal(p.elements.x.checkValidity(), false, 'An empty draft must not become a valid zero when switching eyes');
    await p.reply(); assert.equal(p.elements.x.checkValidity(), false, 'A reply must not replace incomplete numeric input');
    p.input('x', 131); await p.tick(100);
    p.input('top', 'opposite', 'change'); await p.tick(100);
    assert.equal(p.last().data.top, 'opposite'); await p.reply();
    p.elements.center.fire('click'); await p.tick(100);
    assert.equal(p.last().data.center_x, 120); assert.equal(p.last().data.center_y, 160); await p.reply();
    p.elements.preview.fire('keydown', {key:'ArrowRight'}); await p.tick(100);
    assert.equal(p.last().data.center_x, 121); await p.reply();
  }
  {
    const p = await page();
    p.elements.preview.fire('pointerdown', {pointerId:1, clientX:124, clientY:160});
    await p.tick(0); await p.reply();
    p.elements.preview.fire('pointermove', {clientX:125, clientY:160}); await p.tick(16);
    p.elements.preview.fire('pointermove', {clientX:126, clientY:160}); await p.tick(16);
    assert.equal(p.calls.length, 2, 'Start no more than one calibration per 33ms');
    await p.tick(1); assert.equal(p.last().data.center_x, 126);
    assert.equal(p.calls.length, 3, 'Continuous dragging must update before pointerup'); await p.reply();
  }
  {
    const p = await page(), e = p.elements;
    p.input('x', 130); await p.tick(0); p.input('x', 140);
    p.server.eyes[0].center_x = 130; // The timed-out mutation may already have applied.
    await p.reply(false); await p.tick(10000);
    assert.equal(p.calls.length, 2, 'Discard queued moves after an uncertain result');
    p.input('x', 150); await p.tick(1000); assert.equal(p.calls.length, 2, 'Stay paused until a read succeeds');
    assert.equal(e.save.disabled, true); assert.equal(e.refresh.disabled, false);
    e.refresh.fire('click'); assert.equal(p.last().url, '/api/state'); await p.reply();
    assert.equal(e.x.value, '130'); await p.tick(1000); assert.equal(p.calls.length, 3, 'Readback must not replay an old draft');
    p.input('angle', 1.2); await p.tick(0);
    assert.equal(p.last().data.center_x, 130); assert.equal(p.last().data.angle, 1.2); await p.reply();
  }
  {
    const p = await page(true), e = p.elements;
    assert.equal(e.brightness.disabled, false); assert.equal(e.brightness.value, '8');
    p.input('brightness-range', 0); await p.tick(0);
    assert.equal(p.last().url, '/api/backlight');
    assert.deepEqual(p.last().data, {screen:1, brightness:0});
    p.input('brightness-range', 10); p.input('brightness', 25); p.input('angle', 2);
    await p.tick(300); assert.equal(p.calls.length, 2, 'Coalesce brightness while a request is pending');
    await p.reply(); await p.tick(0);
    assert.equal(p.last().url, '/api/calibration');
    assert.deepEqual(p.last().data, {screen:1, center_x:120, center_y:160, angle:2, top:'connector', brightness:25});
    await p.reply();
    p.buttons[1].fire('click'); assert.equal(e.brightness.value, '8');
    p.input('brightness-range', 100); await p.tick(100);
    assert.deepEqual(p.last().data, {screen:2, brightness:100});
    p.buttons[0].fire('click'); assert.equal(e.brightness.value, '25');
    p.input('brightness', 12); await p.reply(); await p.tick(100);
    assert.deepEqual(p.last().data, {screen:1, brightness:12}); await p.reply();
    assert.equal(p.server.eyes[0].brightness, 12); assert.equal(p.server.eyes[1].brightness, 100);
    assert.equal(p.server.eyes[0].angle, 2); assert.equal(p.server.eyes[1].angle, 0);
    assert.equal(p.calls.filter(c => c.url === '/api/save').length, 0);
    e.save.fire('click'); assert.equal(p.last().url, '/api/save'); await p.reply();
    assert.ok(p.server.eyes.every(eye => !eye.unsaved));
    e.reset.fire('click'); assert.equal(p.last().url, '/api/reset'); await p.reply();
    assert.equal(e.brightness.value, '8'); assert.equal(p.server.eyes[1].brightness, 100);
  }
  {
    const p = await page(true), e = p.elements;
    for (const value of ['', -1, 101, 1.5]) {
      p.input('brightness', value); await p.tick(100);
      assert.equal(p.calls.length, 1, 'Invalid brightness must not send');
    }
    p.input('brightness', 0); await p.tick(100); await p.reply();
    p.input('brightness', 20); await p.tick(100); p.input('brightness', 30);
    p.server.eyes[0].brightness = 20;
    await p.reply(false); await p.tick(1000);
    assert.equal(p.calls.length, 3, 'Do not replay an uncertain brightness change');
    assert.equal(e.save.disabled, true); e.refresh.fire('click'); await p.reply();
    assert.equal(e.brightness.value, '20'); await p.tick(1000); assert.equal(p.calls.length, 4);
    p.input('brightness', 8); await p.tick(100); await p.reply();
    assert.equal(p.server.eyes[0].brightness, 8);
  }
  console.log('PASS actual realtime UI script: geometry/backlight, independent screens, 0/full brightness, old firmware guard, coalescing, bounds, save/reset and pause/readback without replay');
})().catch(error => { console.error(error); process.exitCode = 1; });

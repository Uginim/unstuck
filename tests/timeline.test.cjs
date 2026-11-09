const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function setup(count) {
  const items = Array.from({length: count}, () => ({hidden: false}));
  const nodes = {};
  for (const id of ['timeline-controls','timeline-play','timeline-pause','timeline-reset','timeline-status']) {
    nodes['#'+id] = {hidden: true, disabled: false, textContent: '', events: {},
      addEventListener(name, fn) {this.events[name] = fn;}};
  }
  nodes['#timeline-list'] = {querySelectorAll: () => items};
  let tick = null;
  vm.runInNewContext(fs.readFileSync('static/timeline.js','utf8'), {
    document: {querySelector: key => nodes[key]},
    setInterval(fn) { tick=fn; return 1; }, clearInterval() { tick=null; },
  });
  return {items, nodes, click: id => nodes['#timeline-'+id].events.click(), tick: () => tick?.()};
}
test('replay pauses, continues and restores all events without requests', () => {
  const p=setup(3);
  assert.equal(p.nodes['#timeline-controls'].hidden,false);
  assert.ok(p.items.every(item => !item.hidden));
  p.click('play'); assert.deepEqual(p.items.map(i=>i.hidden),[false,true,true]);
  p.click('pause');p.tick();assert.equal(p.items[1].hidden,true);
  p.click('play');assert.equal(p.items[1].hidden,false);
  p.tick();assert.ok(p.items.every(item=>!item.hidden));
  p.click('play');p.click('reset');assert.ok(p.items.every(item=>!item.hidden));
  assert.match(p.nodes['#timeline-status'].textContent,/All 3/);
});
test('empty timeline has no playback controls', () => {
  const p=setup(0);assert.equal(p.nodes['#timeline-controls'].hidden,true);
});

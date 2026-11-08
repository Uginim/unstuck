// Tests the preview's own state changes, not real browser layout or keyboard events.
const test = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const source = readFileSync('static/preview.js', 'utf8');
function page(selectors) {
  let focused;
  const elements = Object.fromEntries(selectors.map(selector => [selector, {
    hidden: true, textContent: '', handlers: {},
    addEventListener(event, handler) { this.handlers[event] = handler; },
    focus() { focused = selector; },
  }]));
  vm.runInNewContext(source, { document: { querySelector: selector => elements[selector] || null } });
  return { elements, focused: () => focused, fire: (selector, event = 'click') => elements[selector].handlers[event]() };
}
test('customer preview requires review again after editing', () => {
  const p = page(['[data-preview-review]', '#confirmation', '#message', '[data-edit-message]', '[data-preview-confirm]', '#confirmation-status']);
  p.fire('[data-preview-review]');
  assert.equal(p.elements['#confirmation'].hidden, false);
  assert.equal(p.focused(), '#confirmation');
  p.fire('[data-preview-confirm]');
  assert.match(p.elements['#confirmation-status'].textContent, /Nothing was saved or sent/);
  p.fire('#message', 'input');
  assert.equal(p.elements['#confirmation'].hidden, true);
  p.fire('[data-preview-review]');
  assert.equal(p.elements['#confirmation-status'].textContent, '');
  p.fire('[data-edit-message]');
  assert.equal(p.focused(), '#message');
  assert.equal(p.elements['#confirmation'].hidden, true);
});
test('approval illustration restores focus on close', () => {
  const p = page(['[data-preview-approval]', '#approval-preview', '[data-close-approval]']);
  p.fire('[data-preview-approval]');
  assert.equal(p.elements['#approval-preview'].hidden, false);
  assert.equal(p.focused(), '#approval-preview');
  p.fire('[data-close-approval]');
  assert.equal(p.elements['#approval-preview'].hidden, true);
  assert.equal(p.focused(), '[data-preview-approval]');
});

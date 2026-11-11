"""Timeline rendering in app.js, run under node: consecutive stop-rule rejections collapse, text stays escaped."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = r"""
const vm = require('node:vm'), fs = require('node:fs');
const el = () => ({addEventListener() {}, classList: {remove() {}, toggle() {}}, style: {}});
const ctx = {document: {querySelector: el, querySelectorAll: () => []}, fetch: () => new Promise(() => {}),
  CSS: {escape: (s) => s}, matchMedia: () => ({matches: false}), setTimeout, clearTimeout, URL, URLSearchParams,
  window: {CASE_ID: 'case'}};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const c = JSON.parse(process.argv[2]);
process.stdout.write(ctx.timelineHtml(c, 0));
"""


def render(events):
    case = {'mode': 'investigate', 'questions': [], 'actions': [], 'events': events}
    return subprocess.run(['node', '-e', PROBE, str(ROOT / 'unstuck/static/app.js'), json.dumps(case)],
                          capture_output=True, text=True, check=True).stdout


def gate(i, reason):
    return {'id': i, 'at': i, 'kind': 'gate', 'label': 'Finish rejected by the stop rule',
            'detail': {'reason': reason, 'attempt': {'cause': 'Tried <b>'}}}


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_consecutive_stop_rule_rejections_collapse_into_one_line():
    read = {'id': 3, 'at': 3, 'kind': 'read', 'label': 'Read reply', 'detail': {'id': 'r'}}
    html = render([gate(1, 'first <x>'), gate(2, 'second'), read, gate(4, 'third')])
    assert html.count('<li class="ev') == 3
    assert 'Stop rule sent it back 2 times' in html and html.count('Finish rejected by the stop rule') == 1
    assert 'first &lt;x&gt;' in html and 'second' in html and 'Tried &lt;b&gt;' in html and '<x>' not in html

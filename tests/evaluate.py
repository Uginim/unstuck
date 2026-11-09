"""Run local-only checks and publish their actual outcomes, including failures."""
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
CHECKS = ROOT / '.checks'
CHECKS.mkdir(exist_ok=True)
revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
run = subprocess.run(['uv', 'run', '--python', '3.13', 'pytest', '-q', '--tb=short',
                      '--basetemp=.checks/pytest-evaluation', '--junitxml=.checks/evaluation.xml'], cwd=ROOT)
if not (CHECKS / 'evaluation.xml').exists():
    raise SystemExit('No evaluation report produced; no result can be claimed.')
items = ET.parse(CHECKS / 'evaluation.xml').getroot().findall('.//testcase')
failed = [item for item in items if item.find('failure') is not None or item.find('error') is not None]
skipped = [item for item in items if item.find('skipped') is not None]
catalog = [item for item in items if item.get('name', '').startswith('test_catalog[')]
catalog_passed = sum(item not in failed and item not in skipped for item in catalog)
lines = [
    '# Dummy results / 더미 결과', '',
    f'Tested code revision: `{revision}`.', '',
    '**Dummy PayPal, not Sandbox. No live PayPal or model calls.**', '',
    f'Operational catalog: **{catalog_passed}/{len(catalog)} scenarios passed**. '
    'Variants inside a scenario are assertions, not separately counted successes.',
    f'Pytest report: {len(items)} reported test cases, {len(failed)} failed, {len(skipped)} skipped. '
    'Pure-policy subtests and supplemental HTTP/model-shape tests are not additional catalog scenarios.', '',
    '## Failures', '',
]
lines.extend(f'- `{item.get("name")}`' for item in failed)
if not failed: lines.append('None.')
lines += ['', '## Limits', '',
          'This run tests the real service and adapter against a synthetic HTTP transport. '
          'It does not validate actual PayPal behavior, credentials, model accuracy, browser layout or deployment. '
          'Independent extraction adversarial cases are a separate evaluation. '
          'A passing scenario does not override failures in supplemental checks.', '',
          'Reproduce with `uv run --python 3.13 python tests/evaluate.py`. '
          'The command exits nonzero on failures and preserves the expected results.', '']
(ROOT / 'tests/results.md').write_text('\n'.join(lines))
sys.exit(run.returncode)

"""Version table pages and downloads after editing the canonical skill-transfer files."""
from pathlib import Path
import hashlib
import json
import re
import time

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / 'skill-transfer'
def digest(data):
    return hashlib.sha256(data).hexdigest()

page = (TABLE / 'index.html').read_text()
page = re.sub(r'<meta name="far-table-(?:revision|generation)" content="[^"]*">', '', page)
page = re.sub(r'<script id="far-table-update"[^>]*></script>', '', page)
script = (ROOT / 'scripts/update-skill-table.js').read_bytes()
script_name = f'update-skill-table.{digest(script)[:16]}.js'
assets = {}
for canonical in ('skill-inventory.svg', 'skill-inventory.pdf', 'skill-inventory.png',
                  'inventory-transfer-matrix.csv', 'inventory-transfer-evidence.csv'):
    stem, extension = canonical.rsplit('.', 1)
    content = (TABLE / canonical).read_bytes()
    filename = f'{stem}.{digest(content)[:16]}.{extension}'
    assets[filename] = content
    page = re.sub(r'href="' + re.escape(stem) + r'(?:\.[a-f0-9]{16})?\.' + extension + r'"',
                  f'href="{filename}"', page)

revision = digest(page.encode() + script)[:16]
previous = json.loads((TABLE / 'revision.json').read_text()) if (TABLE / 'revision.json').exists() else {}
generation = previous.get('generation', 0) if previous.get('revision') == revision else max(int(time.time() * 1000), previous.get('generation', 0) + 1)
document = f'table.{revision}.html'
page = page.replace('</title>', f'</title><meta name="far-table-revision" content="{revision}"><meta name="far-table-generation" content="{generation}">', 1)
page = page.replace('</body>', f'<script id="far-table-update" defer src="../scripts/{script_name}"></script></body>')
home = (ROOT / 'index.html').read_text()
home = re.sub(r'<script id="far-table-update"[^>]*></script>', '', home)
home, count = re.subn(r'<a class="skill-link"[^>]*>', f'<a class="skill-link" data-table-generation="{generation}" href="skill-transfer/{document}">', home)
assert count == 1, 'Expected one existing table link'
home = home.replace('</body>', f'<script id="far-table-update" defer src="scripts/{script_name}"></script></body>')

# Content-addressed outputs are immutable, including when this command is rerun.
outputs = {TABLE / document: page.encode(), ROOT / 'scripts' / script_name: script}
outputs.update({TABLE / name: content for name, content in assets.items()})
for path, content in outputs.items():
    if path.exists() and path.read_bytes() != content:
        raise ValueError(f'Existing immutable release differs: {path.name}')
for path, content in outputs.items():
    if not path.exists():
        path.write_bytes(content)
(TABLE / 'index.html').write_text(page)
(ROOT / 'index.html').write_text(home)
release = {'schema': 'far.skill-table.release.v1', 'revision': revision, 'generation': generation, 'document': document}
(TABLE / 'revision.json').write_text(json.dumps(release, indent=2) + '\n')
print(json.dumps(release))

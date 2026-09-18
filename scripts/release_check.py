"""Check an explicit source inventory. Never print matched secret values."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
IGNORED_DIRS = {'.git', '.venv', '__pycache__', '.pytest_cache', 'node_modules', 'dist'}
IGNORED_FILES = {'.DS_Store'}
# Exact bytes of locally reviewed media, not an extension-wide exemption.
# Any replacement requires a new visual/privacy review before updating this hash.
REVIEWED_MEDIA = {
    'docs/media/basic-interactions.mp4': 'aff19d2c4b69328c2f0647c28d0a0ec240a95a784824b32e0772a3d0ea68a2d7',
}
PATTERNS = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'github-token': re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    'provider-token': re.compile(r'\bsk-[A-Za-z0-9_-]{32,}\b'),
    'personal-machine-path': re.compile(r'(?<![A-Za-z0-9_/])/(?:Users|home)/[A-Za-z0-9_.-]+/'),
    'private-network-address': re.compile(r'\b(?:10\.\d+|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+\b'),
}

def inventory(root=ROOT):
    names = json.loads((root/'public-files.json').read_text())
    if not isinstance(names, list) or len(names) != len(set(names)):
        raise ValueError('Inventory must be a unique list')
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or str(path) != name or any(p in IGNORED_DIRS for p in path.parts):
            raise ValueError('Unsafe inventory entry')
        if path.name == '.env' or path.name.endswith(('.log', '.pyc')):
            raise ValueError('Runtime file in inventory')
        item = root/name
        if not item.is_file() or item.is_symlink() or any(p.is_symlink() for p in item.parents if p != root.parent):
            raise ValueError('Missing file or symlink in inventory')
    return names

def inspect_bytes(name, raw):
    issues = []
    if name in REVIEWED_MEDIA:
        if hashlib.sha256(raw).hexdigest() == REVIEWED_MEDIA[name]:
            return []
        return [{'file': name, 'category': 'reviewed-media-changed'}]
    if name.endswith('.xlsx'):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for member in archive.namelist():
                if member.startswith(('xl/externalLinks/', 'xl/embeddings/')) or member.endswith('vbaProject.bin'):
                    issues.append({'file': name, 'category': 'workbook-external-content'})
                if member.endswith(('.xml', '.rels')):
                    issues += inspect_bytes(name+'!'+member, archive.read(member))
        return issues
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return [{'file': name, 'category': 'unreviewed-binary'}]
    for line, value in enumerate(text.splitlines(), 1):
        for category, pattern in PATTERNS.items():
            if pattern.search(value):
                issues.append({'file': name, 'line': line, 'category': category})
        if '!xl/workbook.xml' in name and re.search(r'state="(?:hidden|veryHidden)"', value):
            issues.append({'file': name, 'line': line, 'category': 'hidden-worksheet'})
    return issues

def check(root=ROOT):
    names = inventory(root)
    issues = []
    for name in names:
        issues += inspect_bytes(name, (root/name).read_bytes())
    expected = set(names)
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        if any(part in IGNORED_DIRS for part in relative.parts) or path.name in IGNORED_FILES:
            continue
        if path.is_symlink():
            issues.append({'file': relative.as_posix(), 'category': 'symlink'})
        elif path.is_file() and relative.as_posix() not in expected:
            issues.append({'file': relative.as_posix(), 'category': 'unlisted-file'})
    return names, issues

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', action='store_true', help='Also require an approved LICENSE; never performs publication')
    args = parser.parse_args()
    names, issues = check()
    if args.release and 'LICENSE' not in names:
        issues.append({'file': 'LICENSE', 'category': 'distribution-approval-pending'})
    print(json.dumps({'files': len(names), 'issues': issues}, ensure_ascii=False, indent=2))
    raise SystemExit(bool(issues))

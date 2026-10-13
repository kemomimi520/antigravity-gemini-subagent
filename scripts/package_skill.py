"""Check and package only public skill source; never include runtime or account data."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile


ROOT_FILES = {'SKILL.md', 'README.md', 'LICENSE', '.gitignore'}
PATTERNS = {
    'google_access_token': re.compile(r'ya29\.[A-Za-z0-9_-]{15,}'),
    'google_refresh_token': re.compile(r'1//[A-Za-z0-9_-]{15,}'),
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'jwt': re.compile(r'eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}'),
    'github_token': re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})'),
    'private_host_path': re.compile(r'(?i)[A-Z]:[\\/]+Users[\\/]+Administrator(?:[\\/]|\b)'),
}


def public_file(relative):
    parts = relative.parts
    if len(parts) == 1:
        return parts[0] in ROOT_FILES
    if len(parts) == 2:
        if parts[0] == 'scripts':
            return relative.suffix in {'.py', '.ps1'}
        if parts[0] == 'references':
            return relative.suffix == '.md'
        if parts[0] == 'tests':
            return parts[1].startswith('test_') and relative.suffix == '.py'
        if parts[0] == 'agents':
            return parts[1] == 'openai.yaml'
    return parts[:2] == ('.github', 'workflows') and len(parts) == 3 and relative.suffix in {'.yml', '.yaml'}


def inspect(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Skill root must be a directory')
    files = []
    findings = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if any(part in {'.git', '__pycache__', '.pytest_cache'} for part in relative.parts):
            continue
        if path.is_symlink():
            findings.append({'file': relative.as_posix(), 'reason': 'symlink_refused'})
            continue
        if not path.is_file():
            continue
        if not public_file(relative):
            findings.append({'file': relative.as_posix(), 'reason': 'not_public_source'})
            continue
        raw = path.read_bytes()
        try:
            lines = raw.decode('utf-8-sig').splitlines()
        except UnicodeError:
            findings.append({'file': relative.as_posix(), 'reason': 'not_utf8_source'})
            continue
        for category, pattern in PATTERNS.items():
            numbers = [number for number, line in enumerate(lines, 1) if pattern.search(line)]
            if numbers:
                findings.append({'file': relative.as_posix(), 'reason': category, 'lines': numbers})
        files.append({'file': relative.as_posix(), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
    for required in ('SKILL.md', 'README.md', 'scripts/workflow.py', 'agents/openai.yaml'):
        if not (root / required).is_file():
            findings.append({'file': required, 'reason': 'required_source_missing'})
    return {'status': 'PASS' if not findings else 'BLOCKED', 'files': files, 'findings': findings,
            'license_specified': (root / 'LICENSE').is_file(),
            'scope': 'source allowlist and common secret patterns; not a complete secret audit'}


def package(root, output):
    root = Path(root).resolve(strict=True)
    output = Path(output).resolve()
    if output == root or root in output.parents:
        raise ValueError('Package output must be outside the skill source directory')
    if output.exists():
        raise FileExistsError('Preserve existing packages; choose a new output name')
    report = inspect(root)
    if report['status'] != 'PASS':
        return report
    output.parent.mkdir(parents=True, exist_ok=True)
    # Pin bytes to the inspected snapshot; concurrent source edits must not enter the ZIP.
    contents = []
    for entry in report['files']:
        path = root / entry['file']
        if path.is_symlink():
            raise ValueError('Source changed to a symlink during packaging')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('Source changed during packaging: ' + entry['file'])
        contents.append((entry['file'], raw))
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in contents:
            info = zipfile.ZipInfo('antigravity-gemini-subagent/' + name, date_time=(2000, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw)
    report['archive_sha256'] = hashlib.sha256(output.read_bytes()).hexdigest()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--output', help='optional new ZIP outside the source tree; otherwise check only')
    args = parser.parse_args()
    try:
        report = package(args.root, args.output) if args.output else inspect(args.root)
    except (OSError, ValueError) as error:
        report = {'status': 'BLOCKED', 'reason': str(error)}
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Check source files without printing credential contents. Not a full secret detector."""
import argparse
import os
from pathlib import Path
import re
import zipfile


SKIP_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache', '.idea', '.vscode'}
PRIVATE_DIRS = {'data', 'runtime', 'kanduweb', 'kanduwebruntime', 'cache', 'covers',
                'books', 'uploads', 'reading_history', 'article_states', 'discussions', 'vocab'}
PRIVATE_NAMES = {'settings.json', 'books.json', 'web-auth.json', 'access-code.txt',
                 'config-auth.json',
                 'reading-stats.json', 'reading-sync-auth.json',
                 'desktop-import.json', 'id_rsa', 'id_ed25519'}
PATTERNS = [
    ('API token pattern', re.compile(rb'\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b')),
    ('GitHub token pattern', re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b')),
    ('AWS access ID pattern', re.compile(rb'\bAKIA[A-Z0-9]{16}\b')),
    ('private key material', re.compile(rb'-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----')),
    ('personal Windows path', re.compile(rb'(?i)[A-Z]:[\\/]+Users[\\/]+(?:Administrator|[^\\/\s"\']+)[\\/]')),
]


def check_bytes(data):
    return [name for name, pattern in PATTERNS if pattern.search(data)]


def find_issues(root):
    root = Path(root).resolve()
    issues = []
    checked = 0
    for current, directories, files in os.walk(root, followlinks=False):
        for name in list(directories):
            path = Path(current) / name
            if path.is_symlink():
                issues.append((str(path.relative_to(root)), 'directory symlink requires manual review'))
                directories.remove(name)
            elif name in SKIP_DIRS:
                directories.remove(name)
        for name in files:
            path = Path(current) / name
            relative = path.relative_to(root)
            label = relative.as_posix()
            checked += 1
            if path.is_symlink():
                issues.append((label, 'file symlink requires manual review'))
                continue
            private_directory = any(p.lower() in PRIVATE_DIRS for p in relative.parts[:-1])
            private_name = (name.lower() in PRIVATE_NAMES or name.startswith('.env')
                or path.suffix.lower() in {'.epub', '.pdf', '.db', '.log', '.pid', '.key', '.pem', '.bak', '.7z', '.rar'}
                or '.sqlite3' in name.lower())
            if private_directory or private_name:
                issues.append((label, 'private runtime file or directory'))
            if path.suffix.lower() == '.zip':
                if label != 'kandu/assets/ecdict_compact.tsv.zip':
                    issues.append((label, 'unexpected archive'))
                    continue
                with zipfile.ZipFile(path) as archive:
                    if archive.namelist() != ['ecdict_compact.tsv']:
                        issues.append((label, 'unexpected dictionary archive contents'))
                    for member in archive.infolist():
                        if member.file_size > 200 * 1024**2:
                            issues.append((label, 'archive entry too large to audit'))
                            continue
                        issues.extend((label + ':' + member.filename, reason)
                                      for reason in check_bytes(archive.read(member)))
            else:
                issues.extend((label, reason) for reason in check_bytes(path.read_bytes()))
    return checked, issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', nargs='?', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    checked, issues = find_issues(args.directory)
    if issues:
        for file, reason in issues:
            print(f'CHECK: {file}: {reason}')
        print(f'FAILED: {len(issues)} issue(s), {checked} files checked. Values are never printed.')
        return 1
    print(f'PASS: {checked} source/resource files; no private runtime artifacts or common credential patterns found.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Conservative source-package checks; not a replacement for a dedicated secret scanner."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '.venv', '.local', '.runtime', 'node_modules', 'dist', '__pycache__'}
SECRET = re.compile(r'(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{25,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')


def source_files():
    for path in sorted(ROOT.rglob('*')):
        relative = path.relative_to(ROOT)
        if path.is_file() and not any(part in SKIP for part in relative.parts):
            yield path, relative


def main():
    findings = []
    for path, relative in source_files():
        if path.name == '.env' or (path.name.startswith('.env.') and path.name != '.env.example'):
            findings.append((str(relative), 'real environment file'))
        if path.suffix.lower() in {'.sqlite', '.sqlite3', '.db', '.log', '.pem', '.key', '.p12', '.zip'}:
            findings.append((str(relative), 'private/runtime artifact'))
        try:
            content = path.read_text(encoding='utf-8-sig')
        except UnicodeError:
            findings.append((str(relative), 'unreviewed binary file'))
            continue
        for line, value in enumerate(content.splitlines(), 1):
            if SECRET.search(value):
                findings.append((str(relative), f'possible credential at line {line}'))
            if re.search(r'(?:[C-Z]:[\\/](?:Users|codex_project|APP)|' + '/' + r'Users/)' , value):
                findings.append((str(relative), f'personal machine path at line {line}'))
        if path.name == 'config.json':
            config = json.loads(content)
            for key, value in config.items():
                if ('api_key' in key or key.endswith('_dsn') or key == 'redis_url') and value:
                    findings.append((str(relative), f'nonempty private configuration: {key}'))
    if findings:
        for file, reason in findings:
            print(f'{file}: {reason}')
        raise SystemExit(1)
    print('Source-package checks passed.')


if __name__ == '__main__':
    main()

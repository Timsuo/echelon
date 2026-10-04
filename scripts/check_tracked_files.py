"""Path-only audit; never opens a secret, database or log."""
import subprocess
from pathlib import PurePosixPath


def forbidden(path):
    if path in {'data/.gitkeep', 'logs/.gitkeep'}:
        return False  # Empty directory placeholders, never runtime contents.
    parts = PurePosixPath(path.lower()).parts
    name = parts[-1]
    return (name == '.env' or name.startswith('.env.') and name != '.env.example'
            or parts[0] in {'data', 'logs', 'attachments', 'venv', '.venv', '.pytest_tmp'}
            or name.endswith(('.db', '.db-wal', '.db-shm', '.sqlite', '.sqlite3')))


def main():
    files = subprocess.check_output(['git', 'ls-files', '-z']).decode('utf-8').split('\0')
    bad = [path for path in files if path and forbidden(path)]
    if bad:
        raise SystemExit('Forbidden tracked local data: ' + ', '.join(bad))
    print('Tracked file safety check passed')


if __name__ == '__main__':
    main()

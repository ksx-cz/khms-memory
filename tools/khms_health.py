"""Refuse a vanished/emptied initialized shared base before a publishing writer runs."""
import sys
from pathlib import Path
import khms_files
import khms_paths as P


def require_healthy(root=None):
    if P.soft('KHMS_SHARED', '0') != '1':
        return
    know = Path(root or P.ROOT) / 'memory/know'
    try:
        cards = [p for p in know.iterdir() if khms_files.canonical(str(p)) and p.name.startswith('K-') and p.is_file()]
        if not cards:
            raise OSError('missing or empty canonical card set')
        for p in cards:
            with p.open('rb') as f:
                if not f.read(1):
                    raise OSError('empty card: ' + p.name)
    except OSError as exc:
        raise RuntimeError('KHMS shared base unhealthy: ' + str(exc)) from exc


if __name__ == '__main__':
    try:
        require_healthy()
    except (RuntimeError, P.KhmsPathError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(3)

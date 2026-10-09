"""Canonical files only; a sync conflict is evidence to report, never a second card."""
import glob as _glob
import os
import re
import sys

CARD = re.compile(r'^K-[0-9]{5}\.md$')
CONFLICT = re.compile(r'conflicted[ _-]copy|conflict(?:ed)?(?:[ _-]|\.)', re.I)


def canonical(path):
    name = os.path.basename(path)
    if CONFLICT.search(name):
        return False
    if name.startswith('K-') and name.endswith('.md'):
        return bool(CARD.fullmatch(name))
    return True


def glob(pattern, **kwargs):
    out = []
    for path in _glob.glob(pattern, **kwargs):
        if canonical(path):
            out.append(path)
        else:
            print('KHMS-CONFLICT: excluded ' + path, file=sys.stderr)
    return out


def listdir(path):
    result = []
    for name in os.listdir(path):
        if canonical(name):
            result.append(name)
        else:
            print('KHMS-CONFLICT: excluded ' + os.path.join(path, name), file=sys.stderr)
    return result


if __name__ == '__main__':
    for directory in sys.argv[1:]:
        for path in sorted(glob(os.path.join(directory, 'K-*.md'))):
            print(os.path.basename(path))

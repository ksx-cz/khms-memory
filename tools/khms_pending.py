"""Read-only lexical recall of unreviewed evidence, separate from approved cards."""
import json
from pathlib import Path
import sys

import khms_paths as P
import khms_search as search
import mark_ledger


def pending():
    paths = mark_ledger.Paths(P.ROOT)
    known = {}
    try:
        with open(paths.marks, encoding='utf-8') as fh:
            for line in fh:
                m = json.loads(line)
                known[m['id']] = m
    except FileNotFoundError:
        pass
    stamp = Path(P.get('KHMS_NIGHT_STAMP'))
    try:
        floor = stamp.read_text().strip()[:10]
    except FileNotFoundError:
        floor = '1970-01-01'
    if not mark_ledger.JOURNAL_NAME_RE.fullmatch(floor+'.md'):
        raise ValueError('night checkpoint is not a date; evidence coverage unknown')
    out = {k:v for k,v in known.items() if v.get('state') == 'OPEN'}
    for mid,m in mark_ledger.collect_marks(paths, floor).items():
        if mid not in known or known[mid].get('state') == 'OPEN':
            out[mid] = m
    return out.values()


def render(query, limit=5):
    terms = set(search.tokens(query))
    try:
        ranked = [(len(terms & set(search.tokens(m['text']))), m) for m in pending()]
    except (OSError, ValueError, KeyError) as exc:
        print(f'UNREVIEWED EVIDENCE: unavailable ({exc}); approved card results follow',file=sys.stderr)
        return 0
    ranked = sorted((x for x in ranked if x[0]), key=lambda x:(-x[0],x[1]['id']))[:limit]
    if ranked:
        print('UNREVIEWED EVIDENCE (lexical; journal findings, not approved knowledge):')
        for _,m in ranked:
            print(f"  {m['id']} {m.get('loc', '?')}: {m['text'][:400]}")
    return len(ranked)

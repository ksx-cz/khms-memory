#!/usr/bin/env python3
"""KHMS dense index builder — one vector per card, incremental.

WHY THIS EXISTS: lexical recall (khms_search.py) is precise on identifiers and
blind to paraphrase. K-00601 decided embeddings are an ADDED channel, never a
replacement — "for symptoms and identifiers lexical search is more precise, an
embedding would blur the exact token that is the signal". This builds the added
channel; fusion and the ship decision live elsewhere.

CARD TEXT = first body line (the card's title) + tags + full body (which already
carries the trailing "CZ:" Czech summary). Frontmatter is stripped by
khms_search.load_cards(), which is REUSED deliberately so the dense pool and the
lexical pool can never drift apart (the same reason recall.sh and khms_hook.py
share one scoring core).

INCREMENTAL: each card carries a content hash; only new/changed cards are
re-embedded. A model-id change invalidates everything (vectors are not
comparable across models).

Storage: memory/.embed/<model-slug>.npz (float16 vectors, ~2 MB per model at
1.1k cards) + <model-slug>.manifest.json. Small enough for the Nextcloud sync;
the WEIGHTS live outside the brain, in ~/models, and must stay there.

FRESHNESS (2026-09-23, EMBSTALE #168): this file used to be a thing somebody had
to REMEMBER to run — cards written during the day were invisible to the dense
half of recall until the 05:10 cron (K-02856, K-02868), silently, which is the
K-00585 shape. It is now called from both sides: `build_views.py` refreshes the
index at the end of every card write, and `khms_recall_hybrid.py` refuses to
answer over an index that is behind without saying so. Both go through
`refresh()` below, and `refresh()` encodes through the ALREADY-WARM embed_server
daemon (`daemon_embed`) so a one-card write costs a few hundred ms instead of the
~9 s a cold sentence-transformers load costs. The cron stays as the backstop.

Usage:
  embed_index.py --model qwen            # build/update (default model)
  embed_index.py --model bge --rebuild   # force full rebuild
  embed_index.py --model qwen --stats    # report only, no work
  embed_index.py --model bge --check     # exit 1 if the index is behind know/
  embed_index.py --model bge --via local # never ask the daemon (cron/rebuild)
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import socket
import sys
import time

# DEVICE: KHMS_EMBED_DEVICE = auto (default, GPU if present else CPU) | cuda | cpu.
# Only an explicit "cpu" hides CUDA before torch is imported; auto/cuda leave the
# GPU visible so load_model() can place the model on it (desktop has ample VRAM).
if os.environ.get("KHMS_EMBED_DEVICE", "auto").strip().lower() == "cpu":
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P  # noqa: E402

if P.soft("KHMS_HF_HOME"):
    os.environ.setdefault("HF_HOME", P.soft("KHMS_HF_HOME"))
import khms_search as ks  # noqa: E402

BRAIN = ks.BRAIN
# KHMS_EMBED_DIR: where the index lives -- shared state by default, or an
# agent-local cache. A reader deployment (KHMS_READ_ONLY=1) never writes a shared
# index: refresh() reports it stale and the recall falls back to lexical.
EMBED_DIR = P.soft("EMBED_DIR")
MAX_CHARS = 4000          # p99 of card bodies is 3926 chars; covers 99% whole
BATCH = 16
# How many cards may be encoded through the daemon in one request. Above this a
# refresh loads the model itself (a full rebuild is a cron/CLI job, not something
# that happens in-line behind a card write or a recall).
DAEMON_MAX = int(os.environ.get("KHMS_EMBED_DAEMON_MAX", "256"))
DAEMON_TIMEOUT_S = float(os.environ.get("KHMS_EMBED_DAEMON_TIMEOUT_S", "120"))
# The interpreter that has torch/sentence-transformers (KHMS_EMBED_PY); unset = this one.
VENV_PY = P.soft("KHMS_EMBED_PY") or sys.executable

MODELS = {
    "qwen": {"hf": "Qwen/Qwen3-Embedding-0.6B", "dim": 1024,
             "query_prefix": "Instruct: Given a search query, retrieve the "
                             "knowledge-base card that answers it\nQuery: ",
             "doc_prefix": ""},
    "bge":  {"hf": "BAAI/bge-m3", "dim": 1024,
             "query_prefix": "", "doc_prefix": ""},
}


def card_text(c):
    """first line (title) + tags + full body. Body already ends with the CZ line."""
    tags = " ".join(c["tags"])
    return f"{c['first']}\n[{tags}]\n{c['body']}"[:MAX_CHARS]


def content_hash(text, model_id):
    return hashlib.sha1(f"{model_id}\x00{text}".encode("utf-8")).hexdigest()[:16]


def paths(slug):
    return f"{EMBED_DIR}/{slug}.npz", f"{EMBED_DIR}/{slug}.manifest.json"


def load_manifest(slug, model_id):
    _, mpath = paths(slug)
    try:
        with open(mpath, encoding="utf-8") as f:
            man = json.load(f)
        if man.get("model_id") != model_id:
            return None
        return man
    except (OSError, ValueError):
        return None


def resolve_device(device=None):
    """auto -> cuda if a usable GPU is present, else cpu."""
    import torch
    if device is None:
        device = os.environ.get("KHMS_EMBED_DEVICE", "auto").strip().lower()
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    return device


def load_model(hf_id, device=None):
    import torch
    device = resolve_device(device)
    if device == "cpu":
        torch.set_num_threads(int(os.environ.get("OMP_NUM_THREADS", "4")))
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(hf_id, device=device)


# ----------------------------------------------------- encoding via the daemon
def dense_configured(slug):
    """Is a dense channel set up at all? An embedding interpreter (KHMS_EMBED_PY) or a daemon socket that
    exists. Without either, dense retrieval is an absent optional feature, reported as such -- not a
    stale index."""
    return bool(P.get("KHMS_EMBED_PY")) or os.path.exists(socket_path(slug))


def socket_path(slug):
    return P.get("KHMS_DENSE_SOCKET") or os.path.join(P.SHARED_STATE, f"embed-{slug}.sock")


def daemon_embed(texts, slug, timeout_s=None, sock_path=None):
    """Encode DOCUMENT texts through the already-warm embed_server daemon.

    The daemon holds the model (8-10 s to load cold, 60 s on CPU), so this is
    what makes an in-line refresh affordable on BOTH the write path and the read
    path. The texts are sent VERBATIM — the caller applies `doc_prefix`, the
    daemon does not, so document and query prefixes can never be swapped by
    accident.

    -> list of vectors. Raises RuntimeError on anything at all: the caller
    decides between a local model load (write path, seconds are fine) and a loud
    warning (read path, where seconds are not).
    """
    timeout_s = DAEMON_TIMEOUT_S if timeout_s is None else timeout_s
    sock_path = sock_path or socket_path(slug)
    req = json.dumps({"op": "embed", "model": slug, "texts": list(texts)}) + "\n"
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(timeout_s)
    buf = b""
    try:
        c.connect(sock_path)
        c.sendall(req.encode("utf-8"))
        while b"\n" not in buf:
            chunk = c.recv(1 << 20)
            if not chunk:
                raise RuntimeError("daemon closed the connection")
            buf += chunk
    except (OSError, socket.timeout) as e:
        raise RuntimeError(f"daemon {type(e).__name__}: {e}") from e
    finally:
        c.close()
    try:
        resp = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    except ValueError as e:
        raise RuntimeError(f"daemon sent unparseable JSON: {e}") from e
    if not resp.get("ok"):
        raise RuntimeError(f"daemon: {resp.get('err', 'not-ok')}")
    if "vectors" not in resp:
        # an OLD daemon answers op=embed as if it were a query (hits, no vectors)
        raise RuntimeError("daemon does not implement op=embed — restart it "
                           "(systemctl --user restart khms-embed.service)")
    if resp.get("model") != slug:
        raise RuntimeError(f"daemon serves model {resp.get('model')!r}, not {slug!r}")
    V = resp["vectors"]
    if len(V) != len(texts):
        raise RuntimeError(f"daemon returned {len(V)} vectors for {len(texts)} texts")
    return V


def encode(payload, slug, via="auto", verbose=True):
    """-> (vectors, how). via: 'auto' (daemon then local) | 'daemon' | 'local'."""
    cfg = MODELS[slug]
    if via in ("auto", "daemon"):
        if len(payload) > DAEMON_MAX:
            if via == "daemon":
                raise RuntimeError(f"{len(payload)} cards exceeds the "
                                   f"{DAEMON_MAX}-card daemon cap")
        else:
            t1 = time.time()
            try:
                V = daemon_embed(payload, slug)
                if verbose:
                    print(f"  daemon encoded {len(payload)} cards in "
                          f"{time.time() - t1:.2f}s (no model load)")
                return V, "daemon"
            except RuntimeError as e:
                if via == "daemon":
                    raise
                if verbose:
                    print(f"  daemon unavailable ({e}) — loading the model locally")
    t0 = time.time()
    model = load_model(cfg["hf"])
    t_load = time.time() - t0
    t1 = time.time()
    V = model.encode(payload, batch_size=BATCH, normalize_embeddings=True,
                     show_progress_bar=False, convert_to_numpy=True)
    t_enc = time.time() - t1
    if verbose:
        print(f"  model load {t_load:.1f}s | encoded {len(payload)} cards in "
              f"{t_enc:.1f}s ({t_enc/max(len(payload),1)*1000:.0f} ms/card)")
    return V, "local"


@contextlib.contextmanager
def index_lock(slug, blocking=False):
    """Serialise writers. Two refreshes at once are not a corruption risk (the
    npz is renamed into place atomically) but they are duplicated GPU work, and
    the loser should say "already running" rather than queue behind it."""
    os.makedirs(EMBED_DIR, exist_ok=True)
    fh = open(f"{EMBED_DIR}/{slug}.lock", "a+")
    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(fh, flags)
    except OSError as e:
        fh.close()
        raise RuntimeError("another index refresh holds the lock") from e
    try:
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def stale_ids(slug, cards=None):
    """How far the index is from know/, in BOTH directions.

    -> (missing_ids, dropped_ids, manifest_or_None, cards)
       missing = on disk, not in the index (new or edited since it was built)
       dropped = in the index, no longer on disk — a GHOST that still answers
                 recall with text nobody can open any more, which is a lie of
                 the same family as the missing card (measured by
                 tools/test_embed_freshness.py, 2026-09-23)
    Pure python, no model: one sha1 per card over text the caller has usually
    already loaded — 7 ms for 3012 cards, cheap enough to sit in front of every
    single recall.
    """
    cfg = MODELS[slug]
    cards = ks.load_cards() if cards is None else cards
    man = load_manifest(slug, cfg["hf"])
    if not man:
        return [c["id"] for c in cards], [], None, cards
    have = man.get("hashes", {})
    missing = [c["id"] for c in cards
               if have.get(c["id"]) != content_hash(card_text(c), cfg["hf"])]
    on_disk = {c["id"] for c in cards}
    dropped = [cid for cid in have if cid not in on_disk]
    return missing, dropped, man, cards


def refresh(slug="bge", cards=None, via="auto", max_cards=None, verbose=False):
    """Bring the index level with know/, or explain why it could not be done.

    -> {"status": "fresh" | "ok" | "failed", "missing", "dropped", "seconds",
        "total", "how", "err", "built", "ids"}
    'failed' is never silent by contract: every caller PRINTS err.
    """
    t0 = time.time()
    try:
        missing, dropped, man, cards = stale_ids(slug, cards)
    except (OSError, ValueError) as e:
        return {"status": "failed", "missing": -1, "dropped": 0,
                "seconds": time.time() - t0,
                "total": 0, "how": None, "built": None, "ids": [],
                "err": f"{type(e).__name__}: {e}"}
    out = {"missing": len(missing), "dropped": len(dropped),
           "total": (man or {}).get("count", 0),
           "built": (man or {}).get("built"), "how": None, "err": None,
           "ids": missing or dropped}
    if not missing and not dropped:
        out.update(status="fresh", seconds=time.time() - t0)
        return out
    if not P.may_write(EMBED_DIR):
        out.update(status="failed", seconds=time.time() - t0, readonly=True,
                   err="read-only deployment: the shared index is not refreshed by a reader")
        return out
    if max_cards is not None and len(missing) > max_cards:
        out.update(status="failed", seconds=time.time() - t0,
                   err=f"{len(missing)} cards behind — more than the "
                       f"{max_cards} this caller will embed in-line")
        return out
    try:
        with index_lock(slug):
            res = build(slug, cards=cards, via=via, verbose=verbose)
    except Exception as e:                                        # noqa: BLE001
        out.update(status="failed", seconds=time.time() - t0,
                   err=f"{type(e).__name__}: {e}")
        return out
    out.update(status="ok", seconds=time.time() - t0, total=res["total"],
               how=res["how"])
    return out


def build(slug, rebuild=False, verbose=True, cards=None, via="auto"):
    import numpy as np
    cfg = MODELS[slug]
    model_id = cfg["hf"]
    os.makedirs(EMBED_DIR, exist_ok=True)
    vpath, mpath = paths(slug)

    cards = ks.load_cards() if cards is None else cards
    texts = {c["id"]: card_text(c) for c in cards}
    hashes = {cid: content_hash(t, model_id) for cid, t in texts.items()}

    old_vecs, old_hash = {}, {}
    man = None if rebuild else load_manifest(slug, model_id)
    if man:
        try:
            # allow_pickle only for indexes written before the ids array became
            # a plain fixed-width unicode array; without it the reload raised
            # ValueError, was swallowed, and every "incremental" run silently
            # became a full 17-50 min rebuild.
            z = np.load(vpath, allow_pickle=True)
            ids, V = list(z["ids"]), z["vectors"]
            old_vecs = {str(i): V[k] for k, i in enumerate(ids)}
            old_hash = man.get("hashes", {})
        except (OSError, ValueError, KeyError):
            old_vecs, old_hash = {}, {}

    todo = [c for c in cards
            if c["id"] not in old_vecs or old_hash.get(c["id"]) != hashes[c["id"]]]
    dropped = [k for k in old_vecs if k not in texts]

    t0 = time.time()
    new_vecs, how = {}, "no-encode"     # dropping a deleted card needs no model
    if todo:
        payload = [cfg["doc_prefix"] + texts[c["id"]] for c in todo]
        V, how = encode(payload, slug, via=via, verbose=verbose)
        for c, v in zip(todo, V):
            new_vecs[c["id"]] = np.asarray(v, dtype="float16")

    merged = {cid: new_vecs.get(cid, old_vecs.get(cid)) for cid in texts}
    ids = sorted(merged)
    V = np.stack([np.asarray(merged[i], dtype="float16") for i in ids])
    fog = {c["id"]: bool(c["fog"]) for c in cards}
    man_new = {"model_id": model_id, "slug": slug, "dim": int(V.shape[1]),
               "count": len(ids), "max_chars": MAX_CHARS,
               "built": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "hashes": hashes, "fog": fog,
               "query_prefix": cfg["query_prefix"]}
    # BOTH files land by atomic rename, and the NPZ lands FIRST. Order matters
    # twice over: the daemon triggers its reload on the npz's mtime (so it can
    # never be handed a half-written matrix), and if the process dies between
    # the two writes the manifest is the OLDER one — which makes the next
    # freshness check say "behind" and refresh again. Manifest-first would have
    # claimed cards were indexed that are not: silent, and the exact failure
    # this whole mechanism exists to remove (K-00585).
    _atomic(vpath, lambda fh: np.savez_compressed(fh, ids=np.array(ids),
                                                  vectors=V))   # '<U9', no pickle
    _atomic(mpath, lambda fh: fh.write(json.dumps(man_new).encode("utf-8")))
    dt = time.time() - t0
    if verbose:
        print(f"  {slug}: {len(ids)} cards ({len(todo)} embedded, "
              f"{len(dropped)} dropped) in {dt:.1f}s -> {vpath} "
              f"({os.path.getsize(vpath)/1e6:.2f} MB)")
    return {"total": len(ids), "embedded": len(todo), "seconds": dt,
            "how": how, "bytes": os.path.getsize(vpath)}


def _atomic(path, write_fn):
    """write_fn(binary file object) -> file appears whole or not at all."""
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        with open(tmp, "wb") as fh:
            write_fn(fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def stats(slug):
    cfg = MODELS[slug]
    man = load_manifest(slug, cfg["hf"])
    if not man:
        print(f"{slug}: no index")
        return 1
    vpath, _ = paths(slug)
    cards = ks.load_cards()
    texts = {c["id"]: card_text(c) for c in cards}
    hashes = {cid: content_hash(t, cfg["hf"]) for cid, t in texts.items()}
    stale = sum(1 for cid, h in hashes.items() if man["hashes"].get(cid) != h)
    print(f"{slug}: {man['count']} vectors dim={man['dim']} built={man['built']} "
          f"{os.path.getsize(vpath)/1e6:.2f} MB | stale/new now: {stale}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen", choices=sorted(MODELS))
    ap.add_argument("--all", action="store_true", help="build every model")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the index is behind know/ (no work done)")
    ap.add_argument("--via", default="auto", choices=("auto", "daemon", "local"),
                    help="auto: warm daemon if it answers, else load the model")
    a = ap.parse_args()
    slugs = sorted(MODELS) if a.all else [a.model]
    if a.stats:
        return max(stats(s) for s in slugs)
    if a.check:
        rc = 0
        for s in slugs:
            missing, dropped, man, cards = stale_ids(s)
            if missing or dropped:
                rc = 1
                print(f"{s}: KHMS-INDEX-STALE — {len(missing)} of {len(cards)} "
                      f"cards not in the index, {len(dropped)} ghost(s) still in "
                      f"it (built {(man or {}).get('built')}): "
                      f"{', '.join(sorted(missing + dropped)[-5:])}")
            else:
                print(f"{s}: up to date ({len(cards)} cards, "
                      f"built {(man or {}).get('built')})")
        return rc
    for s in slugs:
        print(f"[embed_index] {s} ({MODELS[s]['hf']})")
        if not P.may_write(EMBED_DIR):
            P.require_writable(f"build the shared embedding index in {EMBED_DIR}")
        build(s, rebuild=a.rebuild, via=a.via)
    return 0


if __name__ == "__main__":
    sys.exit(main())

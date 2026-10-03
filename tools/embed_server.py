#!/usr/bin/env python3
"""KHMS dense-recall daemon — holds the model and the index warm on a unix socket.

WHY A DAEMON: a cold sentence-transformers load is 4.5 s (Qwen) / 60 s (bge-m3)
on this CPU. That can never sit in the recall path — recall.sh is a habit, and a
habit that costs seconds stops being used. The daemon pays the load once; each
query is then a single forward pass (~65-95 ms) plus a 1113x1024 matmul (<2 ms).

PROTOCOL: newline-delimited JSON over AF_UNIX (SOCK_STREAM).
  request : {"q": "<query text>", "topn": 20}                      (op "query")
  response: {"ok": true, "model": "<slug>", "hits": [["K-90880", 0.71], ...]}
            {"ok": false, "err": "..."}
  request : {"op": "embed", "model": "<slug>", "texts": ["...", ...]}
  response: {"ok": true, "model": "<slug>", "vectors": [[...], ...]}

op=embed (2026-09-23, EMBSTALE #168) is the INDEX WRITER's side of the same warm
model: embed_index.refresh() sends it the few cards that changed instead of
paying a cold 8-10 s sentence-transformers load, which is what makes a refresh
affordable in-line behind every card write AND in front of every recall. Texts
are encoded VERBATIM — the caller applies doc_prefix, never this end, so the
document and query prefixes cannot be swapped by accident.

FRESHNESS: every request re-checks the npz's mtime (one stat) before it is
served, so an index written a millisecond ago is live on the next query. The
30 s poll below remains for an idle daemon.
The client (khms_recall_hybrid.py) uses stdlib socket only — no torch import, no
venv requirement on the caller's side, and a hard timeout so a wedged daemon can
never delay recall past its fallback budget.

DEVICE: KHMS_EMBED_DEVICE = auto (default, GPU if present else CPU) | cuda | cpu.
The desktop has spare VRAM, so the daemon loads the model on CUDA when available;
set KHMS_EMBED_DEVICE=cpu to force CPU (hides CUDA before torch is imported).

Usage:
  embed_server.py --model qwen            # foreground (systemd Type=simple)
  embed_server.py --model qwen --socket /path/to/.embed-qwen.sock
"""
import argparse
import json
import os
import signal
import socket
import sys
import threading
import time

# Only an explicit KHMS_EMBED_DEVICE=cpu hides CUDA before torch import; auto/cuda
# leave the GPU visible so the model can be placed on it (see embed_index.load_model).
if os.environ.get("KHMS_EMBED_DEVICE", "auto").strip().lower() == "cpu":
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P  # noqa: E402

if P.soft("KHMS_HF_HOME"):
    os.environ.setdefault("HF_HOME", P.soft("KHMS_HF_HOME"))
import embed_index as ei  # noqa: E402

BRAIN = ei.BRAIN

RELOAD_POLL = 30.0        # s between manifest mtime checks


class DenseIndex:
    """Model + matrix, reloaded in place when embed_index.py rewrites the npz."""

    def __init__(self, slug):
        import numpy as np
        self.np = np
        self.slug = slug
        self.cfg = ei.MODELS[slug]
        self.lock = threading.Lock()
        self.reload_lock = threading.Lock()
        self.vpath, self.mpath = ei.paths(slug)
        self.mtime = 0.0
        self.model = ei.load_model(self.cfg["hf"])
        self.ids, self.V, self.fog = [], None, {}
        self.load_index()

    def load_index(self):
        np = self.np
        z = np.load(self.vpath, allow_pickle=True)
        ids = [str(x) for x in z["ids"]]
        V = z["vectors"].astype("float32")
        with open(self.mpath, encoding="utf-8") as f:
            man = json.load(f)
        with self.lock:
            self.ids, self.V = ids, V
            self.fog = man.get("fog", {})
            self.mtime = os.path.getmtime(self.vpath)
        return len(ids)

    def maybe_reload(self):
        """One stat. Called before EVERY request as well as from the poller, so
        a card embedded a moment ago is answerable now and not in <=30 s — the
        write path's refresh would otherwise still leave a half-minute hole in
        which recall answers, cleanly, without the new card (EMBSTALE #168)."""
        try:
            if os.path.getmtime(self.vpath) <= self.mtime:
                return
        except OSError:
            return
        with self.reload_lock:
            try:
                if os.path.getmtime(self.vpath) <= self.mtime:
                    return                       # another thread got there first
                n = self.load_index()
                log(f"index reloaded: {n} vectors")
            except (OSError, ValueError, KeyError) as e:
                log(f"index reload FAILED ({type(e).__name__}: {e}) — still "
                    f"serving {len(self.ids)} vectors from {self.mtime}")

    def embed_docs(self, texts):
        """DOCUMENT-side encoding for embed_index.refresh(). Verbatim: no prefix
        is added here (see the module docstring)."""
        if not texts:
            return []
        V = self.model.encode(list(texts), batch_size=ei.BATCH,
                              normalize_embeddings=True, show_progress_bar=False,
                              convert_to_numpy=True)
        return [[float(x) for x in v] for v in V]

    def query(self, text, topn=20):
        np = self.np
        q = self.model.encode([self.cfg["query_prefix"] + text],
                              normalize_embeddings=True, show_progress_bar=False,
                              convert_to_numpy=True)[0].astype("float32")
        with self.lock:
            ids, V, fog = self.ids, self.V, self.fog
        sims = V @ q
        # the lexical channel discounts archived ("fog") cards by 0.6; mirror it
        # so fusion is not fed two differently-shaped pools (khms_search.search)
        if fog:
            mask = np.array([0.6 if fog.get(i) else 1.0 for i in ids],
                            dtype="float32")
            sims = sims * mask
        k = min(topn, len(ids))
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        return [(ids[i], float(sims[i])) for i in idx]


def log(msg):
    sys.stderr.write(f"[embed_server {time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()


def serve(index, sock_path):
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    os.chmod(sock_path, 0o600)
    srv.listen(8)
    log(f"ready on {sock_path} ({len(index.ids)} vectors, model={index.cfg['hf']})")

    def reaper():
        while True:
            time.sleep(RELOAD_POLL)
            index.maybe_reload()
    threading.Thread(target=reaper, daemon=True).start()

    def bye(*_):
        try:
            os.unlink(sock_path)
        except OSError:
            pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)

    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            continue
        threading.Thread(target=handle, args=(conn, index), daemon=True).start()


def handle(conn, index):
    try:
        conn.settimeout(5.0)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
        req = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        t = time.time()
        index.maybe_reload()
        op = req.get("op", "query")
        if op == "embed":
            want = req.get("model")
            if want and want != index.slug:
                raise ValueError(f"this daemon serves {index.slug!r}, not {want!r}")
            vecs = index.embed_docs(req.get("texts") or [])
            resp = {"ok": True, "model": index.slug, "op": "embed",
                    "ms": round((time.time() - t) * 1000, 1), "vectors": vecs}
        elif op == "query":
            hits = index.query(req.get("q", ""), int(req.get("topn", 20)))
            resp = {"ok": True, "model": index.slug, "op": "query",
                    "ms": round((time.time() - t) * 1000, 1), "hits": hits}
        else:
            raise ValueError(f"unknown op {op!r}")
    except Exception as e:                                     # never die on a bad client
        resp = {"ok": False, "err": f"{type(e).__name__}: {e}"}
    try:
        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
    except OSError:
        pass
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen", choices=sorted(ei.MODELS))
    ap.add_argument("--socket", default=None)
    a = ap.parse_args()
    # The daemon runs this clone's code for days: it holds the clone's code-use lock (shared) for its whole
    # life, so an update of the clone refuses until the daemon is stopped in a planned window.
    code_lock = P.hold_code_lock()  # noqa: F841  (kept open for the process lifetime)
    sock = a.socket or ei.socket_path(a.model)
    t = time.time()
    index = DenseIndex(a.model)
    try:
        dev = str(index.model.device)
    except Exception:
        dev = "?"
    log(f"loaded in {time.time()-t:.1f}s (device={dev})")
    serve(index, sock)


if __name__ == "__main__":
    main()

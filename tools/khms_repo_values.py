#!/usr/bin/env python3
"""Index numeric configuration values in an explicitly selected Git repository.
Set KHMS_REPO and optional KHMS_REPO_INCLUDE (space-separated top-level globs).
No checkout is selected by default; read-only git blobs preserve source provenance.
"""
import os
import re
import subprocess
from collections import defaultdict, namedtuple

import yaml

import khms_paths as P
import fnmatch
DEFAULT_REPO = P.soft("KHMS_REPO", "")
Entry = namedtuple("Entry", "name path line raw num")

_SKIP_SEGMENTS = {"test", "tests", "fixtures"}
_HISTORY_LEAVES = {"was"}
_NAME_SPLIT = re.compile(r"[^A-Za-z0-9]+")


def name_words(key):
    """'rear_tyre_track' / 'rear tyre track' / 'Rear-Tyre-Track' -> ('rear','tyre','track')."""
    return tuple(w for w in _NAME_SPLIT.split(str(key).lower()) if w)


def _num(text):
    try:
        v = float(str(text).strip().strip("'\""))
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None


def indexed_path(path):
    parts = path.split("/")
    if not any(fnmatch.fnmatchcase(parts[0], pat) for pat in
               P.soft("KHMS_REPO_INCLUDE", "*").split()):
        return False
    if _SKIP_SEGMENTS & set(parts[:-1]):
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext in (".yaml", ".yml", ".xacro", ".py", ".cpp", ".hpp", ".h"):
        return True
    return ext == ".json" and "config" in parts[:-1]


# --- per-format extractors: text -> [(key name, line, raw, num)] -------------------
_NUM_TAGS = ("tag:yaml.org,2002:int", "tag:yaml.org,2002:float")


def _yaml_entries(text):
    try:
        docs = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    except yaml.YAMLError:
        return []
    out = []

    def leaf_name(path):
        path = [p for p in path if p is not None]
        if path and path[-1] == "value" and len(path) > 1:
            return path[-2]
        return path[-1] if path else None

    def walk(node, path):
        if isinstance(node, yaml.MappingNode):
            for k, v in node.value:
                walk(v, path + [str(k.value)])
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                if isinstance(item, yaml.ScalarNode):
                    walk(item, path + [None])
                # nested lists of lists (footprints, covariance matrices) are not values
                # anyone names in a card one by one — skipped
        elif isinstance(node, yaml.ScalarNode) and node.tag in _NUM_TAGS:
            name = leaf_name(path)
            if name is None or (path and path[-1] in _HISTORY_LEAVES):
                return
            n = _num(node.value)
            if n is not None:
                out.append((name, node.start_mark.line + 1, node.value, n))

    for d in docs:
        if d is not None:
            walk(d, [])
    return out


_XACRO_RE = re.compile(
    r"<xacro:property\s+name\s*=\s*[\"']([\w.]+)[\"']\s+value\s*=\s*[\"']"
    r"\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*[\"']")
_DECL_RE = re.compile(
    r"declare_parameter\s*(?:<[^>()]*>)?\s*\(\s*[\"']([\w./]+)[\"']\s*,\s*"
    r"(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\b")
_LAUNCH_RE = re.compile(
    r"DeclareLaunchArgument\s*\(\s*[\"'](\w+)[\"']\s*,\s*default_value\s*=\s*[\"']?"
    r"\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)[\"']?\s*[,)]")


def _regex_entries(text, regexes):
    out = []
    for rx in regexes:
        for m in rx.finditer(text):
            n = _num(m.group(2))
            if n is not None:
                key = m.group(1).split(".")[-1].split("/")[-1]
                out.append((key, text.count("\n", 0, m.start(1)) + 1, m.group(2), n))
    return out


def _json_entries(text):
    import json
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    lines = text.splitlines()

    def line_of(key):
        for i, ln in enumerate(lines, 1):
            if f'"{key}"' in ln:
                return i
        return 1

    def walk(node, key):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                if not isinstance(v, (dict, list)):
                    walk(v, key)
        elif isinstance(node, (int, float)) and not isinstance(node, bool) and key:
            out.append((key, line_of(key), json.dumps(node), float(node)))

    walk(data, None)
    return out


def file_entries(path, text):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".yaml", ".yml"):
        return _yaml_entries(text)
    if ext == ".xacro":
        return _regex_entries(text, (_XACRO_RE,))
    if ext in (".py", ".cpp", ".hpp", ".h"):
        return _regex_entries(text, (_DECL_RE, _LAUNCH_RE))
    if ext == ".json":
        return _json_entries(text)
    return []


def build_index(files):
    """files: {repo-relative path: text} -> {name (words joined by '_'): [Entry]}.
    Only names of 2+ words are kept (see module doc)."""
    index = defaultdict(list)
    for path, text in sorted(files.items()):
        if not indexed_path(path):
            continue
        for key, line, raw, num in file_entries(path, text):
            words = name_words(key)
            if len(words) < 2:
                continue
            e = Entry("_".join(words), path, line, raw, num)
            if e not in index[e.name]:
                index[e.name].append(e)
    # A code default (declare_parameter / launch argument) is only the fallback when a
    # config file sets the same name — the running value is the file's. Keeping both
    # made "repo says 0.700, -1.0" out of robot_physical.yaml + a sentinel default.
    for name, entries in index.items():
        conf = [e for e in entries if not e.path.endswith(_CODE_EXT)]
        if conf:
            index[name] = conf
    return dict(index)


_CODE_EXT = (".py", ".cpp", ".hpp", ".h")


def read_repo(repo=DEFAULT_REPO, ref="origin/main"):
    """{path: text} for every indexed path at `ref` (fallback HEAD), via git."""
    if not repo:
        return {}
    def git(*args, inp=None):
        return subprocess.run(["git", "-C", repo, *args], input=inp,
                              capture_output=True, check=True).stdout
    try:
        git("rev-parse", "--verify", "-q", ref)
    except (subprocess.CalledProcessError, FileNotFoundError):
        ref = "HEAD"
    try:
        names = git("ls-tree", "-r", "--name-only", ref).decode().splitlines()
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
        return {}
    names = [n for n in names if indexed_path(n)]
    if not names:
        return {}
    blob = git("cat-file", "--batch", inp="".join(f"{ref}:{n}\n" for n in names).encode())
    files, pos = {}, 0
    for n in names:
        nl = blob.index(b"\n", pos)
        header = blob[pos:nl].split()
        pos = nl + 1
        if len(header) < 3 or header[1] == b"missing":
            continue
        size = int(header[2])
        files[n] = blob[pos:pos + size].decode("utf-8", errors="replace")
        pos += size + 1
    return files


_CACHE = {}


def load_index(repo=DEFAULT_REPO, ref="origin/main"):
    """The repo's value index, built once per process ({} when the repo is absent)."""
    key = (repo, ref)
    if key not in _CACHE:
        _CACHE[key] = build_index(read_repo(repo, ref))
    return _CACHE[key]


if __name__ == "__main__":
    import sys
    idx = load_index(*(sys.argv[1:2] or [DEFAULT_REPO]))
    n = sum(len(v) for v in idx.values())
    for name in sorted(idx):
        for e in idx[name]:
            print(f"{name}\t{e.raw}\t{e.path}:{e.line}")
    print(f"khms_repo_values: {len(idx)} names, {n} values", file=sys.stderr)

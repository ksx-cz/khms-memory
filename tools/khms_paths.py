#!/usr/bin/env python3
"""khms_paths.py -- the one place a KHMS tool learns where code, data, state and settings live.

Usage:
  import khms_paths as P; P.KNOW; P.get("KHMS_STATE"); P.require_writable("approve cards")
  python3 khms_paths.py KEY       print one resolved value (rc 3 and a reason on stderr when it cannot)
  python3 khms_paths.py --all     KEY=value for every documented key
  python3 khms_paths.py --root    the resolved data root (same as `KHMS_ROOT`)
  python3 khms_paths.py --code-lock-file   this clone's code-use lock (held shared by night, week, export and
                                  the embedding daemon; an updater takes it exclusively)

Five kinds of location, never derived from one another by accident:
  code          the directory of this file -- a clone of the KHMS repository. Nothing is written here.
  data          KHMS_ROOT: memory/, journal/, plans/, archive/, reports/. Shared by every agent of a base.
  config        KHMS_CONFIG_FILE (settings) and KHMS_CONFIG_DIR (private resources such as a glossary).
  agent state   KHMS_STATE: this agent's logs, hook state and local caches.
  shared state  KHMS_SHARED_STATE: the card allocator, writer locks and night/week checkpoints.

Root discovery, first match wins (there is NO fallback to a checkout or its parent):
  1. $KHMS_ROOT
  2. KHMS_ROOT inside the file named by $KHMS_CONFIG_FILE (the file must exist)
  3. the first line of ~/.config/khms/root (written only by `khms_init.sh --register`)
  otherwise: an error that names khms_init.sh.
Settings file: $KHMS_CONFIG_FILE when set, else ${KHMS_ROOT}/config/khms.env when it exists. A KHMS_ROOT in
that default file that disagrees with the discovered root is an error.

Value precedence for every key: the environment (when set and non-empty), then the settings file, then the
documented default below. An empty value counts as unset.

File grammar (khms_paths.sh reads the same file with the same rules; tests/test_paths.py runs both readers
over the same cases): one KEY=VALUE per line; blank lines and lines starting with `#` are ignored; one
optional pair of surrounding quotes is removed; `${NAME}` is replaced by the RESOLVED value of NAME (env,
file, default). A key may occur only once per file, including identical or empty values. Any other line is
malformed. A reference cycle, a reference to a name nothing defines, a
missing explicitly selected file and a KHMS_READ_ONLY other than 0/1 are errors. The file is never sourced
or evaluated.

Two resolution failures are NOT alike. When nothing selects a base at all -- no KHMS_ROOT, no
KHMS_CONFIG_FILE, no registration -- the error is KhmsNoBaseError, and a tool run on explicit paths only may
proceed: its write policy is then the environment's KHMS_READ_ONLY alone (0 or 1; anything else is an error).
Every other failure is a configuration that WAS selected and cannot be resolved; it stays an error at every
write-policy boundary (read_only, may_write, require_writable) and never implies writer mode.
"""
import os
import re
import sys

CODE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(CODE)

_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# (key, default template). Order matters only for --all output. An empty default means "disabled / not
# configured" for that optional resource; every reader treats it that way.
DEFAULTS = [
    ("KHMS_ROOT", None),                                    # discovered, see above
    ("KHMS_CONFIG_FILE", ""),                               # the selected settings file ("" = none)
    ("KHMS_CONFIG_DIR", "${KHMS_ROOT}/config"),
    ("KHMS_STATE", "${KHMS_ROOT}/state"),
    ("KHMS_SHARED_STATE", "${KHMS_ROOT}/state/shared"),
    ("KHMS_SHARED", "0"),
    ("KHMS_RUNTIME", ""),  # host local; default hashes canonical root, never agent HOME
    ("KHMS_READ_ONLY", "0"),
    ("KHMS_RECALL_LOG", "${KHMS_STATE}/recall.log"),
    ("KHMS_RECALL_JSONL", "${KHMS_STATE}/recall.jsonl"),
    ("KHMS_INJECT_LOG", "${KHMS_STATE}/inject.log"),
    ("KHMS_PRECHECK_LOG", "${KHMS_STATE}/precheck.log"),
    ("KHMS_CLAIM_LOG", "${KHMS_STATE}/claim_gate.log"),
    ("KHMS_PROPOSAL_LOG", "${KHMS_STATE}/proposal_gate.log"),
    ("KHMS_STOP_LOG", "${KHMS_STATE}/stop_gate.log"),
    ("KHMS_RECALL_INTENT", "${KHMS_STATE}/recall_intent.jsonl"),
    ("KHMS_HOOK_STATE", "${KHMS_STATE}/hook-state"),
    ("KHMS_HOOKS_OFF_FILE", "${KHMS_STATE}/hooks-off"),
    ("KHMS_LEN_CACHE", "${KHMS_STATE}/cardlen.json"),
    ("KHMS_EXPERIMENT", "${KHMS_CONFIG_DIR}/khms_experiment.json"),
    ("KHMS_GLOSSARY", ""),
    ("KHMS_DIRECTIVE", ""),
    ("KHMS_EMBED_DIR", "${KHMS_RUNTIME}/embed"),
    ("KHMS_EMBED_PY", ""),
    ("KHMS_DENSE_SOCKET", ""),
    ("KHMS_HF_HOME", ""),
    ("KHMS_TERM_GLOSSARY", ""),
    ("KHMS_REPO", ""),
    ("KHMS_REPO_INCLUDE", "*"),
    ("KHMS_DAV_URL", ""),
    ("KHMS_DAV_USER", ""),
    ("KHMS_DAV_BASE", ""),
    ("KHMS_DAV_NETRC", ""),
    ("KHMS_TRANSCRIPTS", ""),
    ("KHMS_MESSAGES", "${KHMS_CONFIG_DIR}/messages.env"),
    ("KHMS_PATTERNS", "${KHMS_CONFIG_DIR}/patterns.env"),
    ("KHMS_NIGHTLY_LOCK", "${KHMS_RUNTIME}/nightly.lock"),
    # --- night / week pipeline (nightly_distill.sh, weekly_synthesis.sh) ---------------------------------
    ("KHMS_WEEKLY_LOCK", "${KHMS_RUNTIME}/weekly.lock"),     # one weekly at a time
    ("KHMS_NIGHT_STAMP", "${KHMS_SHARED_STATE}/last-nightly"),    # start of the last stamped night window
    ("KHMS_WEEK_STAMP", "${KHMS_SHARED_STATE}/last-weekly"),      # completion of the last weekly
    ("KHMS_SWEEP_FAILED", "${KHMS_SHARED_STATE}/sweep-failed"),   # one line per failed sweep, cleared on success
    ("KHMS_NIGHT_LOG", "${KHMS_STATE}/nightly.log"),              # night AND week log
    ("KHMS_AGENT", "claude"),                                     # headless agent CLI (argv[0])
    ("KHMS_AGENT_TRANSCRIPTS", "${HOME}/.claude/projects"),       # where that CLI writes session transcripts
    ("KHMS_SWEEP_MODEL", "sonnet"),
    ("KHMS_MARK_MODEL", "sonnet"),
    ("KHMS_CONS_MODEL", "sonnet"),
    ("KHMS_WEEK_PART_MODEL", "sonnet"),
    ("KHMS_WEEK_MODEL", "opus"),
    ("KHMS_PROJECT_SLUG", ""),             # only transcript paths containing this text ("" = every transcript)
    ("KHMS_GIT_REPOS", ""),                # space-separated repositories whose commits are a night source
    ("KHMS_CHAT_FETCH", ""),               # argv: refresh the chat archive before the night ("" = off)
    ("KHMS_CHAT_VERIFY", ""),              # argv: verify principal quotes in the journal ("" = off)
    ("KHMS_NIGHT_TRIAGE", ""),             # argv: the standing-base triage stage ("" = off, said in the log)
    ("KHMS_SPEC", ""),                     # card schema the prompts cite ("" = spec/khms-spec.md of this clone)
    ("KHMS_PRINCIPAL", "the principal"),   # who the base serves, as the prompts name them
    ("KHMS_PROMPT_EXT", "${KHMS_CONFIG_DIR}/prompts"),            # private prompt extensions (optional)
    ("KHMS_ACCOUNT_DIR", ""),              # token accounting: the project's transcript directory ("" = derive)
    # --- morning review ping (khms_review_ping.sh) -----------------------------------------------------
    ("KHMS_PING_DEPUTY", ""),
    ("KHMS_REVIEW_DEPUTY_TRANSPORT", ""),
    ("KHMS_JUDGE_EXPERIMENT_CONFIG", ""),
    ("KHMS_METRICS_CONFIG", ""),
    ("KHMS_PING_SEND", ""),                # argv: delivers the ping text read on stdin ("" = print only)
    ("KHMS_PING_EXTRA", ""),               # argv: project lines to add to the ping, one per stdout line ("" = none)
    # --- standing-base triage (nightly_triage.sh, khms_contradict.py) -----------------------------------
    ("KHMS_JUDGE", "sonnet"),              # pair judge of record: qwen (local KHMS_LLM_URL) | a KHMS_AGENT model
    ("KHMS_JUDGE_MAX_USD", ""),            # estimated per-night bound of a paid judge ("" = no bound)
    ("KHMS_LLM_URL", ""),                  # local model endpoint (Ollama /api/generate; "" = no local model)
    ("KHMS_LLM_MODEL", "qwen3-coder-next:latest"),               # the local model's name
    ("KHMS_REPORT_TOOL", ""),                               # regex of the report tool ("" = built-in default)
    ("KHMS_REPORT_CHANNEL", ""),                            # regex: prompts that open a report turn ("" = all)
    ("KHMS_REPORT_SERVER", ""),                             # channel server name part ("" = built-in default)
]
KEYS = [k for k, _ in DEFAULTS]
_DEFAULT = dict(DEFAULTS)


class KhmsPathError(LookupError):
    """A location that cannot be resolved; the message says which and why."""


class KhmsNoBaseError(KhmsPathError):
    """Nothing selects a base (no KHMS_ROOT, no KHMS_CONFIG_FILE, no registration). The one resolution
    failure that is not a configuration error: explicit-path library use is intentionally unconfigured."""


def registered_root_file(env=None):
    env = os.environ if env is None else env
    home = env.get("HOME") or ""
    return os.path.join(home, ".config", "khms", "root") if home else ""


def parse_config(text, origin="<config>"):
    """[(key, raw value)] in file order; reject malformed lines and duplicate keys."""
    out = []
    first_lines = {}
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _LINE_RE.match(s)
        if not m:
            raise KhmsPathError(f"khms_paths: malformed line {n} in {origin} (expected KEY=VALUE)")
        key = m.group(1)
        if key in first_lines:
            raise KhmsPathError(
                f"khms_paths: duplicate key {key} at line {n} in {origin} "
                f"(first defined at line {first_lines[key]})")
        first_lines[key] = n
        val = m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out.append((key, val))
    return out


def _read_file(path, required):
    try:
        with open(path, encoding="utf-8") as fh:
            return parse_config(fh.read(), path)
    except FileNotFoundError:
        if required:
            raise KhmsPathError(f"khms_paths: config file {path} does not exist (named by KHMS_CONFIG_FILE)")
        return []
    except OSError as exc:
        raise KhmsPathError(f"khms_paths: cannot read config file {path}: {exc.strerror}")


def _norm(p):
    return p.rstrip("/") or "/"


class Resolved:
    """All locations for one environment. Built once per process (see `resolved()`)."""

    def __init__(self, env=None):
        self.env = dict(os.environ if env is None else env)
        cfg = self.env.get("KHMS_CONFIG_FILE") or ""
        self.raw = dict(_read_file(cfg, True)) if cfg else {}
        self.cache = {}
        root = self.env.get("KHMS_ROOT") or ""
        if not root and cfg and self.raw.get("KHMS_ROOT"):
            root = self._expand("KHMS_ROOT", self.raw["KHMS_ROOT"], ("KHMS_ROOT",))
        registered = False
        if not root:
            reg = registered_root_file(self.env)
            if reg and os.path.isfile(reg):
                registered = True
                with open(reg, encoding="utf-8") as fh:
                    root = (fh.readline() or "").strip()
        if not root:
            if cfg:
                raise KhmsPathError(
                    f"khms_paths: the settings file {cfg} (KHMS_CONFIG_FILE) names no KHMS_ROOT and no base is "
                    "registered -- add KHMS_ROOT to it, set KHMS_ROOT, or create and register a base with: "
                    "khms_init.sh --register <dir>")
            if registered:
                raise KhmsPathError(
                    f"khms_paths: the registration {reg} is empty -- run khms_init.sh --register <dir>")
            raise KhmsNoBaseError(
                "khms_paths: no KHMS data root -- set KHMS_ROOT, name a settings file with KHMS_CONFIG_FILE, "
                "or create and register a base with: khms_init.sh --register <dir>")
        if not os.path.isabs(root):
            raise KhmsPathError(f"khms_paths: KHMS_ROOT must be an absolute path, got: {root}")
        self.cache["KHMS_ROOT"] = root
        if not cfg:
            default_cfg = os.path.join(root, "config", "khms.env")
            if os.path.isfile(default_cfg):
                cfg = default_cfg
                self.raw = dict(_read_file(cfg, False))
                if self.raw.get("KHMS_ROOT"):
                    other = self._expand("KHMS_ROOT", self.raw["KHMS_ROOT"], ("KHMS_ROOT",))
                    if _norm(other) != _norm(root):
                        raise KhmsPathError(
                            f"khms_paths: {cfg} names KHMS_ROOT={other} but the discovered root is {root}")
        self.cache["KHMS_CONFIG_FILE"] = cfg
        ro = self.get("KHMS_READ_ONLY")
        if ro not in ("0", "1"):
            raise KhmsPathError(f"khms_paths: KHMS_READ_ONLY must be 0 or 1, got: {ro}")
        self._validate_shared()

    def _validate_shared(self):
        if self.get('KHMS_SHARED') != '1':
            return
        state = self.env.get('KHMS_STATE')
        if not state or not os.path.isabs(state) or under(state, self.get('KHMS_ROOT')):
            raise KhmsPathError('shared base requires an explicit personal KHMS_STATE outside KHMS_ROOT')
        # Shared runtime is part of the base configuration. Two agent environments
        # must not redirect the same lock to different files.
        configured = self.raw.get('KHMS_RUNTIME')
        if not configured:
            raise KhmsPathError('shared base requires KHMS_RUNTIME in the base configuration')
        intended = self._expand('KHMS_RUNTIME', configured, ('KHMS_RUNTIME',))
        actual = self.get('KHMS_RUNTIME')
        if not os.path.isabs(actual) or under(actual, self.get('KHMS_ROOT')) or not same_path(actual, intended):
            raise KhmsPathError('KHMS_RUNTIME must match the shared absolute path outside KHMS_ROOT')
        for key in ('KHMS_SHARED_STATE', 'KHMS_NIGHTLY_LOCK', 'KHMS_WEEKLY_LOCK'):
            raw = self.raw.get(key) or _DEFAULT[key]
            intended = self._expand(key, raw, (key,))
            if not same_path(self.get(key), intended):
                raise KhmsPathError(key + ' must match the shared base configuration')


    def _expand(self, key, raw, stack):
        def sub(m):
            name = m.group(1)
            if name in stack:
                raise KhmsPathError(
                    f"khms_paths: {stack[0]} refers to itself through {' -> '.join(stack + (name,))}")
            return self._resolve(name, stack + (name,))
        return _REF_RE.sub(sub, raw)

    def _resolve(self, key, stack):
        if key in self.cache:
            return self.cache[key]
        val = self.env.get(key)
        if not val:
            if self.raw.get(key):
                val = self._expand(key, self.raw[key], stack)
            elif key == "KHMS_RUNTIME":
                from khms_runtime import default_runtime
                val = default_runtime(self.get("KHMS_ROOT"))
            elif key in _DEFAULT and _DEFAULT[key] is not None:
                val = self._expand(key, _DEFAULT[key], stack)
            else:
                raise KhmsPathError(
                    f"khms_paths: {stack[0]} refers to ${{{key}}}, which nothing defines")
        self.cache[key] = val
        return val

    def get(self, key):
        return self._resolve(key, (key,))

    @property
    def read_only(self):
        return self.get("KHMS_READ_ONLY") == "1"


_RESOLVED = None


def resolved():
    global _RESOLVED
    if _RESOLVED is None:
        _RESOLVED = Resolved()
    return _RESOLVED


def reset():
    """Forget the cached resolution (tests that change os.environ in-process)."""
    global _RESOLVED
    _RESOLVED = None


def get(key):
    return resolved().get(key)


def read_only():
    """Is this a reader deployment? When nothing selects a base (a tool run on explicit paths only) the
    environment alone decides. A selected configuration that cannot be resolved raises KhmsPathError: an
    invalid deployment is never a writer."""
    try:
        return resolved().read_only
    except KhmsNoBaseError:
        ro = os.environ.get("KHMS_READ_ONLY") or "0"
        if ro not in ("0", "1"):
            raise KhmsPathError(f"khms_paths: KHMS_READ_ONLY must be 0 or 1, got: {ro}")
        return ro == "1"


EXIT_READ_ONLY = 4


def require_writable(what):
    """Refuse a data/shared-state mutation BEFORE any write when this deployment is a reader."""
    if read_only():
        print(f"khms: read-only deployment (KHMS_READ_ONLY=1): refusing to {what}; nothing was written",
              file=sys.stderr)
        sys.exit(EXIT_READ_ONLY)


def may_write(path):
    """May this process append to `path` (a log, a cache)? Always on a writer deployment. On a reader
    (KHMS_READ_ONLY=1) only outside the data root, the config dir and the shared state: a reader keeps its
    logs and caches in its own KHMS_STATE, which must then live outside the shared base."""
    if not read_only():
        return True
    try:
        r = resolved()
    except KhmsNoBaseError:          # an environment-only reader with no base: nothing is shared, write nothing
        return False
    return not any(under(path, r.get(k)) for k in ("KHMS_ROOT", "KHMS_CONFIG_DIR", "KHMS_SHARED_STATE", "KHMS_RUNTIME"))


def require_may_write(paths, what):
    """Refuse (rc 4, before any write) when this deployment may not write one of `paths`.

    For a tool that is not a base mutator but writes files a caller names (a report, a preview, a
    checkpoint): a writer may write anywhere; a reader only outside the data root, the config dir and the
    shared state (`may_write`, real paths -- an alias of a protected path is that path). Every output of
    the run is checked here, not only the main one. Empty/None entries are skipped."""
    for p in paths:
        if p and not may_write(p):
            print(f"khms: read-only deployment (KHMS_READ_ONLY=1): refusing to {what}: {p} is inside the "
                  "base, its configuration or its shared state; nothing was written", file=sys.stderr)
            sys.exit(EXIT_READ_ONLY)


def same_path(a, b):
    """Do `a` and `b` name the same location (real paths)? A path is compared by what it names, never as
    text: `<base>/.` or a symlink to the base IS the base."""
    return bool(a) and bool(b) and os.path.realpath(a) == os.path.realpath(b)


def under(path, base):
    """True when `path` is `base` or inside it (both made absolute, symlinks resolved)."""
    p, b = os.path.realpath(path), os.path.realpath(base)
    return p == b or p.startswith(b.rstrip("/") + "/")


# --- the fixed layout beneath the roots: a key names a root, not every file ---------------------------------
_DERIVED = {
    "ROOT": lambda r: r.get("KHMS_ROOT"),
    "MEM": lambda r: os.path.join(r.get("KHMS_ROOT"), "memory"),
    "KNOW": lambda r: os.path.join(r.get("KHMS_ROOT"), "memory", "know"),
    "VIEWS": lambda r: os.path.join(r.get("KHMS_ROOT"), "memory", "views"),
    "INBOX": lambda r: os.path.join(r.get("KHMS_ROOT"), "memory", "inbox"),
    "STAGING": lambda r: os.path.join(r.get("KHMS_ROOT"), "memory", "inbox", ".staging"),
    "ARCHIVE_KNOW": lambda r: os.path.join(r.get("KHMS_ROOT"), "memory", "archive", "know"),
    "TOMBSTONES": lambda r: os.path.join(r.get("KHMS_ROOT"), "memory", "tombstones"),
    "MEMORY_MD": lambda r: os.path.join(r.get("KHMS_ROOT"), "memory", "MEMORY.md"),
    "JOURNAL": lambda r: os.path.join(r.get("KHMS_ROOT"), "journal"),
    "PLANS": lambda r: os.path.join(r.get("KHMS_ROOT"), "plans"),
    "CONFIG_DIR": lambda r: r.get("KHMS_CONFIG_DIR"),
    "STATE": lambda r: r.get("KHMS_STATE"),
    "SHARED_STATE": lambda r: r.get("KHMS_SHARED_STATE"),
    "COUNTER": lambda r: os.path.join(r.get("KHMS_SHARED_STATE"), "next_id"),
    "ALLOC_LOCK": lambda r: os.path.join(r.get("KHMS_RUNTIME"), "alloc.lock"),
    "RUNTIME": lambda r: r.get("KHMS_RUNTIME"),
    "REVIEW_MARKERS": lambda r: os.path.join(r.get("KHMS_SHARED_STATE"), "reviewed"),
    "RECALL_LOG": lambda r: r.get("KHMS_RECALL_LOG"),
    "RECALL_JSONL": lambda r: r.get("KHMS_RECALL_JSONL"),
    "INJECT_LOG": lambda r: r.get("KHMS_INJECT_LOG"),
    "PRECHECK_LOG": lambda r: r.get("KHMS_PRECHECK_LOG"),
    "CLAIM_LOG": lambda r: r.get("KHMS_CLAIM_LOG"),
    "HOOK_STATE": lambda r: r.get("KHMS_HOOK_STATE"),
    "HOOKS_OFF": lambda r: r.get("KHMS_HOOKS_OFF_FILE"),
    "LEN_CACHE": lambda r: r.get("KHMS_LEN_CACHE"),
    "EXPERIMENT": lambda r: r.get("KHMS_EXPERIMENT"),
    "GLOSSARY": lambda r: r.get("KHMS_GLOSSARY"),
    "EMBED_DIR": lambda r: r.get("KHMS_EMBED_DIR"),
    "DIRECTIVE_FILE": lambda r: (r.get("KHMS_DIRECTIVE")
                                 or os.path.join(CODE, "prompts", "report_directive.md")),
    # Before the split this was "<root>/tools", which held code and state at once. Its remaining users locate
    # CODE (scripts beside this module); state has its own keys above.
    "TOOLS_DIR": lambda r: CODE,
    "TOOLS": lambda r: CODE,
}


EXIT_CODE_BUSY = 5


def code_lock_file():
    """The code-use lock file of THIS clone (khms_paths.sh computes the same path).

    ONE place per clone, derived from the clone alone and never from the configuration: a lock whose path
    followed the selected base or agent state let two configurations of the same clone take the shared (user)
    and the exclusive (updater) lock at once -- each locked its own file. So the lock lives in the clone's own
    git directory (`<gitdir>/khms-code.lock`; a linked worktree has its own gitdir, and its own code), found
    from the real path of this file (a symlinked tool or clone resolves to the same lock). A tree that is not a
    git checkout uses `<clone>/.khms-code.lock`. No setting can move it, so no two users of one clone can
    disagree about it."""
    repo = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    dotgit = os.path.join(repo, ".git")
    if os.path.isdir(dotgit):
        return os.path.join(dotgit, "khms-code.lock")
    if os.path.isfile(dotgit):
        with open(dotgit, encoding="utf-8") as fh:
            first = fh.readline().strip()
        if first.startswith("gitdir:"):
            gd = first[len("gitdir:"):].strip()
            return os.path.join(os.path.realpath(os.path.join(repo, gd)), "khms-code.lock")
    return os.path.join(repo, ".khms-code.lock")


def hold_code_lock():
    """Hold a SHARED lock on this clone's code for the life of the returned file object (see
    khms_code_lock in khms_paths.sh). Raises KhmsPathError when an updater holds it exclusively."""
    import fcntl
    path = code_lock_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise KhmsPathError(f"khms: the code clone {CODE} is being updated (exclusive lock {path} is held) "
                            "-- refusing to start")
    if fh.tell() == 0:
        fh.write(f"code={CODE}\n")
        fh.flush()
    return fh


def _excepthook(etype, value, tb, _orig=sys.excepthook):
    """An unresolvable location is a configuration answer, not a crash: one line and rc 3."""
    if issubclass(etype, KhmsPathError):
        sys.stdout.flush()
        print(str(value), file=sys.stderr)
        sys.stderr.flush()
        os._exit(3)
    _orig(etype, value, tb)


sys.excepthook = _excepthook


def soft(name, default=None):
    """P.<name> (a derived location or a KHMS_* key), or `default` when no base is selected. For
    module-level defaults of library modules that must stay importable without a base; any real use of the
    base still fails with the explanatory error, and a selected but invalid configuration fails here."""
    try:
        return get(name) if name.startswith("KHMS_") else _DERIVED[name](resolved())
    except KhmsNoBaseError:
        return default


def __getattr__(name):           # PEP 562: P.KNOW etc. resolve on first use, not at import
    if name in _DERIVED:
        return _DERIVED[name](resolved())
    raise AttributeError(f"module 'khms_paths' has no attribute {name!r}")


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip().split("\n\n")[0])
        return 0 if argv else 2
    try:
        r = resolved()
        if argv[0] == "--all":
            for k in KEYS:
                print(f"{k}={r.get(k)}")
            return 0
        if argv[0] == "--code-lock-file":
            print(code_lock_file())
            return 0
        if argv[0] == "--root":
            print(r.get("KHMS_ROOT"))
            return 0
        print(r.get(argv[0]) if argv[0].startswith("KHMS_") else _DERIVED[argv[0]](r))
        return 0
    except KeyError:
        print(f"khms_paths: unknown name {argv[0]}", file=sys.stderr)
        return 2
    except KhmsPathError as exc:
        print(str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

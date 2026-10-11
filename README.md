# KHMS — a file-based long-term memory an agent can install into itself

KHMS ("know-how management system") is a long-term memory for LLM agents made of plain
markdown files — in a plain directory with server-side file version history for
the deployments that will not put their memory in a repository at all. Every piece of knowledge is one immutable **card** with
YAML frontmatter — what kind of knowledge it is, how strongly it is evidenced, where it came
from, and which other cards it is derived from, supports or contradicts. Cards are never
edited and never deleted: a correction is a new card that *supersedes* the old one, and a
refuted card stays visible as a signposted dead end. Around that storage layer sits the part
that makes it work in practice — **hook-driven recall** that pushes relevant cards into the
session before the agent asks for them, and a **propose → review → approve** pipeline in
which background jobs may only propose and nothing enters the knowledge directory unreviewed.

Status: extracted and generalized from a working single-operator deployment that has been
running daily since mid-2026. The scripts here are the deployment's scripts with the paths
parameterized and the domain specifics removed. Numbers marked "calibrate" are that
deployment's values, not laws.

## Read next

- **Setting up memory for yourself?** → **[AGENTS.md](AGENTS.md)** — the one-pass bootstrap.
- Wondering whether the automatic recall is worth its context? →
  [docs/measuring-injection.md](docs/measuring-injection.md) — measure it, do not argue it.
- Want the whole model first? → [spec/khms-spec.md](spec/khms-spec.md).
- Wiring it into Claude Code (hooks, cron, dependencies)? → [claude-code/](claude-code/README.md).
- Want to see cards before writing any? → [examples/](examples/) (fictional weather-station domain).

## Quick start

Python 3, PyYAML, git and Linux shell utilities are sufficient for this local
check. Provision PyYAML first (for example, install your distribution's
`python3-yaml` package). Keep the clone separate from the data directory:

```bash
git clone https://github.com/ksx-cz/khms-memory.git "$HOME/khms"
export KHMS_CODE="$HOME/khms"
export KHMS_ROOT="$HOME/.agent-memory"
"$KHMS_CODE/tools/khms_init.sh" --register "$KHMS_ROOT"
python3 "$KHMS_CODE/tools/build_views.py"
python3 "$KHMS_CODE/tools/mark_ledger.py" sync
"$KHMS_CODE/tools/recall.sh" --lexical "checksum mismatch"
printf '%s\n' '{"hook_event_name":"SessionStart","session_id":"install-check"}' \
  | python3 "$KHMS_CODE/tools/khms_hook.py"
tail "$(python3 "$KHMS_CODE/tools/khms_paths.py" KHMS_INJECT_LOG)"
```

The ledger sync establishes MARK coverage from the journal before the first hook
check. Run it again after journal changes to refresh coverage; it approves no cards.

No cards are approved by initialization. An empty base returns "nothing on record"
with recall exit 1; it is a successful empty search. Lexical recall needs no model or
embedding service. Registration is scoped to HOME; hooks running as another user
need their own registration or explicit `KHMS_ROOT`. Continue with [AGENTS.md](AGENTS.md)
for reviewed bootstrap cards and [Claude Code wiring](claude-code/README.md).

## Safe code updates

Update the clone, leaving the data base in place. Pause scheduled jobs and active
users of this clone first. A code-use lock prevents a running nightly, weekly,
triage or export stage from overlapping the update; pause hooks and interactive
commands too. Explicitly stop the embedding daemon for this clone before the
update: it holds the code-use lock for its entire lifetime, so waiting for an idle
daemon will never release the lock. Use the deployment's normal daemon stop command.
Check that `git -C "$KHMS_CODE" status --porcelain` is empty, then:

```bash
git -C "$KHMS_CODE" fetch origin
# Set this to the full commit SHA approved for release, with green test.yml CI for that SHA.
approved_sha="<approved-full-commit-sha>"
lock="$(python3 "$KHMS_CODE/tools/khms_paths.py" --code-lock-file)"
flock -n -x "$lock" git -C "$KHMS_CODE" merge --ff-only "$approved_sha"
```

A lock refusal means the clone is still in use. Identify the holder; let a finite
job finish or stop the embedding daemon through its normal lifecycle command. Do
not retry indefinitely. The release gate is [.github/workflows/test.yml](.github/workflows/test.yml)
for that exact SHA. Do not select a moving branch tip after checking CI. A git
refusal means the revision or history needs review; do not force reset.
Keep the exclusive lock held for the whole code change, not only a check before it.
After success, repeat the lexical recall and hook checks above, inspect the release
notes for any explicit migration, then restart the embedding daemon through its
normal lifecycle command and resume paused jobs. The base's configuration,
cards, journal and state are retained; no code is copied into the base and no publish
manifest is needed. A reader deployment can update its code without making data writable.

## For humans: what problem this solves

An agent that works with someone for months keeps re-deriving the same conclusions, repeating
documented dead ends, and stating yesterday's fact as today's. Context windows do not fix this
— they are per-session and they are lossy. Vector-store "memory" mostly fixes recall of *text*,
not the harder parts: whether a remembered claim was measured or merely reported, what refuted
it, and who approved it into the record.

KHMS's answers, in one line each:

- **Cards, not chat logs.** One claim per file, typed (`fact`, `problem→solution`,
  `decision→rationale`, `principle`, `policy`, …), so knowledge can be linked and counted.
- **Epistemic levels.** Observations carry `evidence: measured | observed | reported` and a
  source; rules are `derived` and must name what they were derived from. Confidence is
  *computed* from that graph, never hand-asserted.
- **Immutability.** No edits, no deletions. Corrections supersede; refutations stay readable,
  because "we already tried that and it failed" is among the most valuable things memory holds.
- **Retrieval as a floor plus a ceiling.** Hooks inject candidate cards automatically on a
  budget (the floor, which runs whether or not the agent remembers to look); explicit recall
  before hypotheses and proposals is the agent's own duty (the ceiling).
- **Graduated review.** Cheap models propose into an inbox, a stronger stage consolidates,
  and only an approving stage assigns IDs and writes into the knowledge directory.

### Prior art and further reading

- **Google Cloud's Open Knowledge Format (OKF)** — the closest thing to a standard for this
  storage layer: a directory of markdown files with YAML frontmatter, one required field
  (`type`), no runtime. KHMS's card storage converged on nearly the same shape independently;
  what OKF (v0.1 June 2026, v0.2 July 2026) does not define is the epistemic and process layer
  above it — evidence levels, mandatory provenance, refuted-not-deleted, computed belief, the
  propose→review cycle. If you want KHMS cards to interoperate, map the frontmatter.
  Spec: <https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md> ·
  announcement: <https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing>
- **LLM Wiki** (Andrej Karpathy) — a pattern for LLM-built and LLM-maintained personal
  knowledge bases: a persistent, compounding, interlinked wiki instead of per-query RAG.
  The closest articulation of the idea this system grew from:
  <https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f>
- Papers this design was built against (each is one card's worth of the argument):
  - Generative Agents (reflection: periodically distilling observations into higher-level
    conclusions) — <https://arxiv.org/abs/2304.03442>
  - Mem0 (memory operations for LLM agents, incl. deletion of contradicted memories — KHMS
    deliberately does the opposite) — <https://arxiv.org/html/2504.19413>
  - Sleep-time compute (doing the distillation work between sessions, not during them) —
    <https://arxiv.org/abs/2504.13171>
  - Graphiti (temporal knowledge graph for agents; invalidation rather than deletion, at the
    cost of requiring a graph database) — <https://github.com/getzep/graphiti>

## License

MIT — see [LICENSE](LICENSE).

### One shared base, separate agents

Set `KHMS_SHARED=1` in the base's configuration, with one absolute `KHMS_RUNTIME`
outside the synchronized root. Each agent must supply its own `KHMS_STATE`, also
outside the root. Runtime locks and the embedding cache are host-local; counters,
night/week checkpoints, review markers and approval receipts live in
`KHMS_SHARED_STATE` and travel with the base. Shared paths cannot be overridden by
one agent's environment. An initialized shared base with no canonical cards is a
failure, not a fresh base to recreate.

Approval, triage and errata hold the same card lock from validation through the
last write. Approval receipts reserve IDs once and support retry after a partial
write; a different input for the same unfinished unit is refused. View builders
hold that lock while reading and publishing complete files. A durable dirty mark
survives a crash before the rebuild. Conflicted copies are reported and excluded
from retrieval, pending review, allocation and export.

Recall prints matching unreviewed journal findings in a separate evidence section;
they are not approved cards. Proposal gates show relevant recall results once per
author context; terminology rules can be supplied with `KHMS_TERM_GLOSSARY`.
`khms_review_state.py` supplies the pending census. `khms_review_ping.sh --deputy`
delegates delivery to the explicitly configured `KHMS_PING_DEPUTY` command.

There are no local snapshots or pre-write backups. `khms_versions.py` lists and
reads retained Nextcloud versions, and restores an explicitly selected version
with byte verification. Configure `KHMS_DAV_URL` (including `/remote.php/dav`),
`KHMS_DAV_USER`, `KHMS_DAV_BASE` and `KHMS_DAV_NETRC`. Credentials are read only by
curl from that file. A listed but unreadable version is not recovery evidence.
Restore while writers are paused, reconcile the counter and tombstone ledger,
check the review hashes, and rebuild the views before resuming. Per-file history
is not an atomic history of the whole base. Missing history remains unavailable;
it cannot be reconstructed as evidence from a derived packet or today's card.

### The MARK stage runs first and keeps what it finished

The OPEN journal MARKs of the coverage ledger (`mark_ledger.py queue`) are the
night's FIRST model stage, before the sweep and whatever the sweep does (fail,
time out, find nothing, find hundreds of candidates). It has its own budget,
`MARK_BUDGET_MIN` counted from the stage's own start, and no gate on how many
candidates already wait for consolidation; the night's other deadlines are
unchanged. Every finished chunk — candidates and `## MARKS NOT CARDED` verdicts —
is committed on its own, by one rename, as the carry directory
`memory/ledger/carry/<night key>-mNN/` (no `WINDOW` file, so it never moves the
sweep's stamp) and is published by the consolidation as units
`<night key>-mNN-bKK.md`. A chunk killed before its commit leaves its marks OPEN
(queued again, nothing of it used); a committed chunk counts as proposed and is
never queued again.

### A night longer than its budget resumes, it does not start over

The nightly sweep reads one window of sources (since the night stamp) in parts,
one fresh model session per part (`SWEEP_GROUP_CHUNKS`). When a window has more
parts than one night's `SWEEP_BUDGET_MIN` covers, the finished parts are kept:

- Before part 1, the run freezes the window's input list in
  `memory/inbox/.staging/.prepare-<night key>/FROZEN` — `since=`, `end=` (the
  window), `group_chunks=`, `parts=` and `index_sha256=` of the ordered input list
  `nightly-<night key>-prepare-chunks.txt`, plus one `input=<sha256><TAB><path>`
  line per frozen input — every primary chunk and the window's own digest, git
  log, their chunk indices and journals — so the record vouches for the inputs'
  CONTENT, not only their names. The window's inputs stay in staging under that
  run's night key.
- Each finished part gets a completion record `done-NNN` beside `FROZEN`, renamed
  into place after its call and reassembly: the sha256 of its part list and of its
  output `nightly-<night key>-sweep-gNN.md`. A part without a valid record (killed,
  timed out, cut by the deadline) is redone from nothing; a recorded part never is.
- The next run finds the frozen list, collects no new window, runs only the
  parts without a record, and builds the sweep file from all part outputs in
  order. Only when the whole list is complete and committed to the carry queue
  does the stamp move — to the frozen `end`, so the next window starts exactly
  there and no input is skipped. A list the stamp already covers is removed; a
  damaged one (an input missing, or its content sha256 not the frozen one) is set
  aside WHOLE as `.damaged-<time>` before any completion record is used, and its
  window is collected again from the unmoved stamp: a changed chunk no longer
  matches the digest it was cut from or the grounding the finished parts quoted,
  so redoing just its part could not be shown to sweep the window. The ledger
  grounding `nightly-<night key>-journals-ledger.md` is not frozen — a resume
  refreshes it on purpose (journals only grow).

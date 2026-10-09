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

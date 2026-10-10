# KHMS specification

This specification is itself meant to live in the knowledge base as one `policy` card
(`tags: [khms, core]`), because of P1 below. Changes to it are made the same way any other
correction is made: a new policy card with `SUPERSEDES <old-id> BECAUSE: …`, derived from an
observation that documents what failed. Not by editing this file in place.

## 1 Purpose

Capture everything experienced, distill it into evidence-linked knowledge, and support
decisions from all of it — the internal knowledge base first, external sources (manuals,
vendor documentation, forums, papers, the web) whenever a fact needs verification or the base
is silent.

## 2 Principles

- **P1 One mechanism.** Everything is a card, including this specification.
- **P2 Immutability, no deletion.** Approved cards never change; corrections are new cards
  (`supersedes`). Knowledge only changes `status` or moves to the greppable archive ("fog").
  The raw layer (transcripts, exports) is append-only, forever.
- **P3 Descriptive vs. normative.** What happened → observation-level cards. What to do →
  derived-level cards linked by `derived_from`. A recommendation can die without killing its
  fact; one fact can feed competing recommendations.
- **P4 No hard thresholds.** Continuous quantities and ranked lists instead of cutoffs.
  Numeric constants are calibratable configuration (§7), not rules.
- **P5 Frugality.** At every step the cheapest sufficient instrument: script → cheap model →
  mid model → strong model.
- **P6 Graduated review.** A lower-capability stage prepares, a higher-capability stage
  approves; only approved content enters `memory/know/` or changes a status.
- **P7 One language inside the base.** Cards, views, journal and inbox are written in a single
  language (English in the reference deployment), whatever language anyone speaks with the
  agent. If the operator's language differs, see §4.11.

## 3 Storage layout

```
MEMORY.md                 generated bootstrap index, always in context (§8)
memory/know/K-*.md        cards
memory/views/             generated: topics/<tag>.md, by-type/<type>.md, tags.md, recent.md
memory/inbox/             job proposals awaiting review (temp labels, no IDs)
memory/inbox/.staging/    exactly what each pipeline stage was handed (forensic record)
memory/archive/know/      condensed and superseded cards — "fog", greppable
archive/                  raw layer: transcripts, chat exports, git dumps; append-only
journal/YYYY-MM-DD.md     MARK anchors appended during work + the factual day summary
                          appended by the nightly sweep
tools/                    scripts; tools/.next_id — the ID counter
```

## 4 Card attributes

One card = one file: YAML frontmatter + body. Body length follows content: atomic knowledge is
typically 2–8 lines; `policy` and `overview` cards may be long (this spec is one card).

### 4.1 id
`K-NNNNN`, sequential, never reused. Assigned only at review, from `tools/.next_id` (single
writer). Inbox candidates carry temporary labels (`C1`, `C2`, …) until then.

### 4.2 type
Shape of the knowledge; determines the body template.

| `type` | Meaning | Use when | Body template | Level |
|---|---|---|---|---|
| `action→outcome` | Causal event: doing X in context C produced Y | an attempt had a result (worked / failed / partial — state it in THEN) | `WHEN:` `THEN:` | observation |
| `problem→solution` | A solved case | a symptom was diagnosed and the fix verified | `SYMPTOM:` `CAUSE:` `FIX:` `VERIFIED:` | observation |
| `fact` | Static reality with exact values | IDs, addresses, paths, configurations, properties, external-source findings | free statement | observation |
| `requirement` | A stated demand | someone expressed a need | `WHO:` `WHAT:` `WHY:` `DONE-CRITERIA:` | observation |
| `decision→rationale` | A decision that was made | an alternative was chosen | `DECIDED:` `WHY:` `REJECTED:` | observation |
| `principle` | Empirical pattern | ≥2 independent observations share a shape | `HOLDS:` `LIMITS:` `IMPLICATIONS:` | derived |
| `policy` | Normative rule or specification | a way of working is adopted | `RULE:` `LIMITS:` `IMPLICATIONS:` (long form allowed) | derived |
| `goal→method` | Reusable how-to | a repeatable method exists | `GOAL:` `METHOD:` `PREREQUISITES:` `COST:` | derived |
| `overview` | Topic map / entry point | a topic needs an anchor and links | short narrative + links | derived |

Observation templates are descriptive by construction; normative wording exists only in derived
templates. The type set is open: adding a type = one `decision→rationale` card + a row here.

Worked example — the system described by itself:

- This spec = one `policy` card (long form), `derived_from` the prior-art overview card and the
  design decision cards.
- A prior-art survey = one `overview` card linking atomic `fact` observations, each with
  `evidence: reported` and a `source` naming a URL plus a locating quote ("system X requires a
  running graph database", "system Y deletes contradicted memories"). Each finding is
  independently refutable; the full report file is the overview's `source`.
- Changing the system: an observation documents what failed and how; a new `policy` card is
  written with `derived_from: [that observation, the old spec]` and a body line
  `SUPERSEDES K-old BECAUSE: …`; the old spec card goes to the archive as `superseded`.

### 4.3 level
| Value | Meaning |
|---|---|
| `observation` | Record of what happened or was stated: experiment, measurement, event, quote, external-source finding. Requires `evidence` and `source`. |
| `derived` | Induced content: rules, principles, methods, maps. Requires non-empty `derived_from`. Credibility is the computed `belief` (§5), never hand-set. |
| `assumption` | Hypothesis without evidence yet; expected to gain support or be refuted. |

### 4.4 status
| Value | Meaning | Set by |
|---|---|---|
| `active` | In force. Default after approval. | review |
| `challenged` | A contradicting card arrived; resolution pending (experiment or review). Stays visible, marked. | review, on a job's flag |
| `refuted` | Killed by stronger evidence (`refuted_by` filled). Stays in `know/` as a signposted dead end. | review |
| `superseded` | Replaced via a `supersedes` chain. Moves to the archive. | review, together with the replacement |
| `condensed` | Absorbed into a pattern. Moves to the archive ("fog"). | weekly review |

Status transitions happen only at review. Jobs propose them; they do not perform them.

### 4.5 tags
Flat associative keys — what the card touches; any number per card. Values come from the
registry `memory/views/tags.md` (tag, group, one-line description, aliases, count).
`build_views.py` normalizes aliases and lists unregistered tags at the top of the registry; new
tags are adopted at review. Two conventional tags carry behaviour: `gotcha` (surfaced by
`precheck.sh` and protected from condensation) and `core` (§9.6).

### 4.6 scope
Position in a tree ontology of generality — *where* the knowledge holds. One scope per card.
The tree grows as needed; it is not a closed enum. Shape:

```
universal                       holds beyond any domain (method, process, physics)
└─ <domain>                     e.g. home-automation
   ├─ platform:<x>              a platform or stack
   ├─ device:<x>                a device model
   └─ project:<x>               one project
```

Scope vs. tags: scope is a single position on the generality axis (where it applies); tags are
flat associations (what it touches). Seeding a new project from an old base: take everything
except the old `project:*` cards.

### 4.7 evidence
Observation cards only; the input weight for belief (§5).

| Value | Meaning |
|---|---|
| `measured` | Done, measured or reproduced by us. |
| `observed` | Seen once, not reproduced. |
| `reported` | Stated by a person or an external source (documentation, manual, forum, paper); not verified by us. |

### 4.8 source
Provenance: file / journal reference / session id / URL, **plus a locating quote**. Required
for observations. A source you cannot locate the claim in is not a source.

### 4.9 date
When the knowledge was established — not when the card was written. ISO date.

### 4.10 links
| Link | Meaning |
|---|---|
| `derived_from: [K-*]` | Evidence this card is induced from. Feeds its belief support. Required non-empty for `derived`. |
| `supports: [K-*]` | This card adds support to the listed cards' belief. |
| `contradicts: [K-*]` | Mutual conflict (symmetric). Feeds the oppose side of belief on both ends. |
| `supersedes: K-*` | This card replaces an older one. The body must contain `SUPERSEDES K-x BECAUSE: <reason>`. |
| `refuted_by: [K-*]` | Evidence that killed this card. Filled when status → refuted. |

These five are the only link types. A relation that is not one of them belongs in prose, where
a human reads it — not in a sixth key that no tool will ever read. Links must live inside the
`links:` mapping; a top-level `supports:` is silently ignored by every consumer.

**A correction MUST carry its edge.** A card whose body says it corrects something (`CORRECTED`,
`SUPERSEDES`, "contrary to", "was wrong", "no longer true", and the operator language's
equivalents) and names nothing in `contradicts` / `supersedes` / `refuted_by` is refused at
approval by `tools/khms_lint.py` — before any id is allocated — with a message naming the missing
edge. Retrieval travels by edges: a correction written only as prose inside the correcting card
cannot be reached from the card it corrects, which therefore goes on being served alone, as
current. If the corrected claim was genuinely never carded, the body says so in one line —
`NO-CORRECTION-TARGET: <reason>` — which is a statement in an immutable card, not a flag in a
script. The other half of the same rule lives in retrieval (§6): every served card is served with
whatever corrects it, **whatever its status**, because a correction always arrives before anyone
re-statuses the old card.

### 4.11 Operator-language line (optional)
When the operator's working language is not the language of the base, end every card body with
one line in the operator's language summarising the card, and quote the operator's own words
verbatim when the source contains them. Rationale: later retrieval is lexical, and the
operator's future questions will be phrased in their own words — a base written only in the
base language cannot be matched by them. It is a translation of the card's grounded content,
never a place for new facts.

## 5 Computed quantities (deterministic, `build_views.py`)

```
weight: measured→3, observed→2, reported/assumption→1                      (config §7)
support(D) = Σ weight(active cards in D.derived_from ∪ {cards whose supports ∋ D})
             + w_indirect per active derived card among them
oppose(D)  = Σ weight(active cards in D.contradicts ∪ D.refuted_by)
belief(D)  = tanh((support − oppose) / k)          derived cards only, shown with counts

condense_score(O) = weeks_untouched × (absorbed ? 1.0 : f_unabsorbed)
                    absorbed := O ∈ derived_from of ≥1 active pattern
                    protected → 0 (see §7)
```

belief: 0 = no or balanced evidence; ±1 = an unreachable asymptote; negative = evidence against
dominates (a challenge or refutation candidate). The weekly review consumes the condense
ranking top-down; there is no cutoff (P4).

## 6 Retrieval

| Step | Instrument | Cost |
|---|---|---|
| 0 | `MEMORY.md` (already in context) | 0 |
| 1 | `memory/views/topics/<tag>.md` | 1 read |
| 2 | `tools/precheck.sh <tags>` — active policies + gotchas + refuted/challenged; a duty before risky actions | 0 model tokens |
| 3 | `tools/recall.sh <free text>` / grep `memory/know/` | ~0 |
| 4 | full-text index (optional, when the base outgrows a linear scan) | ~0 |
| 5 | embedding index (optional) | low |
| 6 | fog: grep `memory/archive/know/`, zgrep the raw archive | ~0 |
| 7 | external sources: documentation in the repository, web search and fetch, vendor forums | tokens |

Step 7 is **mandatory** when asserting facts not covered by `measured` cards, and when the base
is silent. External findings enter the base as `reported` observations with URL sources.

Retrieval has two layers with different failure modes:

- **The floor** — automatic injection by harness hooks, on a budget. It fires without anyone
  remembering it. It is *bounded* (thresholds, rate cap, per-card dedup), so "it would have
  popped up by itself" is not a guarantee of anything. One thing it is *not* allowed to drop:
  a card is never served without a one-line pointer to whatever corrects it (§4.10), whatever
  either card's status, and those pointers do not compete with the character budget.
- **The ceiling** — explicit `recall.sh` before root-causing, before stating a hypothesis and
  before making a proposal, and `precheck.sh` before risky actions. The hook cannot fire on
  what the agent has not yet said, which is exactly what a proposal is.

## 7 Configuration (calibratable parameters, not rules)

| Parameter | Reference default | Used by |
|---|---|---|
| evidence weights | measured 3, observed 2, reported/assumption 1 | belief |
| `w_indirect` | 2 | belief |
| `k` (belief slope) | 4 | belief |
| `f_unabsorbed` | 0.3 | condense_score |
| condense-protected | `fact` cards; `gotcha`-tagged; refuted; policy; overview | condense_score |
| injection threshold | 18 (operator message), 15–16 (tool events) | hook |
| second-card minimum / max cards per injection | 14 / 2 | hook |
| injection char cap | 900 | hook |
| rate cap | 3 injections per 10 min, +1 bypass ride for a top hit ≥ the message threshold | hook |
| dedup TTL per card per session | 12 h | hook |
| length damping | `score × min(1, (pivot/distinct_tokens)^0.5)`, pivot = the base's median | hook |
| repeat-query cooldown | 15 min | hook |
| stage roles | prepare: cheap model; consolidate: mid model; approve: main session or human; arbitrate: operator | processes, P6 |
| stage wall-clock timeouts | 90 min sweep, 100 min consolidate | pipeline |
| monthly token budget (kill switch) | set during calibration | background jobs |

Values are what one deployment converged on by measurement; they are starting points to
recalibrate against your own logs, and recalibrations are recorded as cards.

## 8 Generated artifacts

- `memory/views/topics/<tag>.md` — Patterns (by belief, with evidence counts) / Facts &
  observations / Gotchas / Challenged / ⛔ Refuted & dead ends.
- `memory/views/by-type/<type>.md`, `memory/views/recent.md`.
- `memory/views/tags.md` — the registry: tag, group, description, aliases, count; with an
  UNREGISTERED section on top.
- `memory/views/condense-candidates.md`, `memory/views/scopes.md`.
- `MEMORY.md` (≤80 lines, loaded into every context):
  `1 How this KB works` (the body of the bootstrap-digest card) · `2 Core` (every `core`-tagged
  card, one line each) · `3 Current focus` (active plans, latest journal) · `4 Top patterns`
  (top 5 by belief) · `5 Topics` (tag groups with counts).

## 9 Processes

### 9.1 Session capture
On a decision, a stated requirement, a failed attempt or a solved problem: append one line to
today's journal — `MARK[kind] one-line summary (sess id)`. A full inline card only for
`measured` knowledge with immediate usefulness. Every actual journal MARK, including an unfamiliar
kind, enters the coverage ledger and is owned by the nightly record stage.

### 9.2 Nightly sweep (daily, after transcript archival)
Prepare inputs: a delta transcript digest (message texts, including plan passages, tool names,
exit states and errors — tool outputs and thinking stay in the raw layer) and the day's git log,
with explicit MARK records excluded. Prepare never reads journals; journal MARKs belong solely
to the record stage. Large inputs are split into
chunks that the reading tool can actually read whole; an input handed over in one oversized
file is read as a self-chosen prefix, silently.

Stages: (a) prepare extracts unmarked knowledge as candidate cards per §4; the record stage
processes queued journal MARKs by stable id, producing a candidate or an explicit NOT CARDED reason
for each. Status-only records remain accounted for without requiring a knowledge card.
Over-generation is fine; verbatim source quotes are mandatory. (b) a mechanical quote verifier greps every quote against
the source it names and lists unbacked specifics; (c) a mid-tier model enforces that report,
deduplicates, enforces the schema, links candidates to existing cards using the topic views of
the touched tags, flags contradictions and cross-topic synergies, and appends a factual day
summary to the journal.

Output: `memory/inbox/DATE.md` — `## Cards` (temp labels), `## Flagged`, `## Dropped`.
Proposals only. The sweep never touches `memory/know/`.

### 9.3 Morning review (first full session of the day)
Strong attention on `## Flagged`; approve, fix or reject the rest quickly. Assign IDs, write
into `memory/know/`, run `build_views.py`. Ends with a short report to the operator in the
operator's language.

### 9.4 Weekly synthesis (weekly, off-hours)
Inputs: all card headers, the week's flags, the week's inboxes, the ranked condense candidates.
Proposes: new `principle` cards where ≥3 independent observations share a shape and lack an
umbrella; for contradicting pattern pairs, belief and evidence side by side plus the cheapest
discriminating experiment; condensations (with a preservation check — the absorbing pattern
must carry all condensed information); deduplications and supersessions. Output:
`memory/inbox/DATE-weekly.md`.

### 9.5 Weekly review
As 9.3, plus: confirm condensations (move to fog; the absorbing pattern keeps a one-line digest
annotation), and record calibration notes on the §7 parameters.

### 9.6 Core promotion
At review, a card that has proved stable and is structurally or normatively significant gets
the `core` tag, and `MEMORY.md` regenerates. Demotion works the same way in reverse.

### 9.7 Escalation
Pattern conflicts that affect real decisions, refutation of a requirement, and changes to core
cards go to the operator as a short question. The outcome is recorded as a card.

## 10 Failure modes this design is built against

Each of these was paid for once; they are why the odd-looking parts are the way they are.

- **A fluent card with invented specifics.** A structurally perfect card whose numbers, log
  lines and "VERIFIED:" claims never existed. Countermeasure: mandatory verbatim quotes plus a
  mechanical verifier — a check, not a request (§9.2b).
- **A check that passes either way.** A gate asked "is this covered by its source?" of a stage
  that was never handed the source. A check whose passing condition holds whether or not the
  thing it guards is healthy carries no evidential weight.
- **A silent partial import.** A card that failed to parse produced one stderr line and exit
  code 0, so a partial import read as a complete one. Loading tools exit non-zero and say what
  was lost.
- **A pipeline that fails invisibly.** A stage that hangs holds its lock forever, and every
  later run then exits "already running" without doing anything — one lost night becomes all of
  them. Every stage has a wall-clock timeout, and staleness of the completion stamp is itself
  alerted on.
- **Retrieval that never fires.** A rule saying "consult the base first" fails by not firing,
  never by being wrong. Countermeasure: move the duty into the hook layer where possible, and
  make every non-firing visible in the audit log with its reason.
- **A card that stores a live tunable's value.** The value moves; the card cannot notice, and
  decays into confident misinformation. Store where the value lives, not what it is.

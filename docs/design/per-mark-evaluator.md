# Noční zpracování poznámek: jedno rozhodnutí na jednu poznámku (#237, krok 1)

**Co je špatně.** V noci dostane jazykový model (sonnet) najednou 60 holých řádků poznámek z deníku a ke
každé si sám, krok za krokem, dohledává okolí v deníku a podobné karty v bázi. Jedna skupina tak stojí
44–103 kroků, 4–9 minut a 0,79–2,12 dolaru. Potom ještě jedno volání na skupinu návrhy slučuje (16 volání,
12,18 dolaru za noc 13. 10.). Ráno přesto vypadlo 88 ze 106 návrhů vzniklých z poznámek: byly to buď
duplikáty karet, nebo nebyly know-how.

**Proč.** Model nedostane podklady předem, takže je pokaždé hledá znovu a ne vždy najde tu správnou kartu.
Karty nejbližší podle vyhledávání se počítají až po něm, takže jsou k dispozici až slučovacímu kroku.

**Co změníme.** Program ke každé poznámce předem a bez modelu přiloží okolí v deníku, nejbližší karty z báze
(a karty, které poznámka sama jmenuje), návrhy, které ta noc už udělala, a u poznámek, které odkazují na konkrétní
rozhovor, i výřez z jeho přepisu. Model pak jednou, bez nástrojů, vrátí jedno rozhodnutí: zahodit s důvodem, nová
karta, patří k už navrženému, už je v kartě (s doslovnou citací), nebo nahrazuje či opravuje existující
kartu. Jeho návrh je rovnou hotový pro ranní schválení. Samostatné slučovací volání pro návrhy z poznámek
proto odpadá.

**Jak poznáme, že to funguje.** Jednu noc porovnáme s 13. 10.: dolary, minuty a počet kroků na 60 poznámek,
počet návrhů na poznámku a hlavně kolik návrhů z poznámek ráno vypadne (13. 10. to bylo 88 ze 106).
Cena za jednu poznámku vyjde podle odhadu zhruba stejně jako dnes (asi 2 dolary za 60 poznámek); zisk má
být v kvalitě návrhů a v menším ranním vyřazování. Druhý krok (později): stejným vyhodnocovačem projdou i
nálezy z přepisů rozhovorů a z historie gitu, a zmizí tak i druhé slučování.

---

## Goal
One model call per queued journal MARK, no tools, one decision out, from a packet a script assembles in
advance with zero tokens. The candidate it writes is approve-ready: it already carries the RELATION and the
quotes. The separate consolidate call for mark candidates goes away (owner, 13. 10. 08:07). The ledger, the
candidate format, `convert_inbox2.py`, `verify_relations.py`, `approve_inbox.py` and the
`mark_ledger.py dismiss --from-inbox` path keep working unchanged. Step 2, the transcript sweep feeding the
same evaluator, is costed in its own section and is not built now.

Structure: 1 today's state · 2 the design (prefetch, call, decisions, driver) · 3 the decision formats
with worked examples from the 2026-10-13 night · 4 what is left out · 5 measurement plan · 6 cost ·
7 step 2 · 8 implementation checklist · 9 KHMS · 10 owner questions.

## 1 Today's state (khms-memory `origin/master` 50f88fd, rechecked 2026-10-13)
Paths starting `tools/`, `tests/`, `docs/` and `spec/` are in this repository. Paths starting `journal/`,
`plans/` and `memory/` are in the deployment's KHMS data root (here the brain
`/home/jiri-hanus/Devel/thorin/.thorin-brain`).

| what | where |
|---|---|
| chunk size 60, per-call timeout 30 min, stage budget 90 min | `tools/nightly_distill.sh:190`, `:191`, `:203` |
| stage isolation: `--setting-sources "" --effort high --strict-mcp-config --tools Read Write Edit Glob Grep` | `tools/nightly_distill.sh:687` |
| the MARK stage loop, one agent session per 60-mark chunk | `tools/nightly_distill.sh:933-1017` |
| the chunk commit: relabel, `nearest_cards.py` (AFTER the model), quote check, `split-run` into the carry queue | `tools/nightly_distill.sh:959-976` |
| the prompt: bare queue lines; the agent Greps the journal and `memory/know/` itself | `tools/prompts/nightly-marks.md:14-23` |
| the contract (every id in exactly one place), candidate format, header shape, quote rules | `tools/prompts/nightly-marks.md:25-88` |
| consolidation of every carry batch, mark batches included: one agent call per batch | `tools/nightly_distill.sh:1404-1414` |
| post-checks of a consolidated batch: attribute, quote check, know-how count, `verify_relations` | `tools/nightly_distill.sh:1290-1312` |
| a batch whose durable output `cons-NN.out.md` exists is published WITHOUT a model call | `tools/nightly_distill.sh:1385-1395` |
| batch identity: `carry/<key>/cons-NN.md` → durable output `cons-NN.out.md` | `tools/nightly_distill.sh:597-611` |
| one-shot `claude -p` without tools (`--tools ""`, `--system-prompt`, `--no-session-persistence`, prompt on stdin) | `tools/khms_contradict.py:1249-1253`, runner seam `:151` |
| retrieval used for NEAREST: lexical + dense hybrid, active non-fog cards only | `tools/nearest_cards.py:95`, `:120`, `:158` |
| the quote test the morning dismiss path applies | `tools/mark_ledger.py:1021-1041` |
| transcript digest with UTC timestamps per record | `tools/preprocess_transcripts.py:205` |

Measured on the 2026-10-13 night (`~/.local/state/ops/khms/nightly.log`, Thorin's home, lines 1553-1962):
17 chunks = 1,020 marks in 92.6 min, $21.67 and 1,141 turns (CLI `total_cost_usd`, from the
`marks-N served-by:` lines). Per chunk: 44–103 turns, 3.9–8.5 min, $0.79–2.12. Out came 125 candidates and
809 NOT CARDED lines (459 already carded, 304 not know-how, 39 duplicates, 7 unproven). The 16
`consolidate-2026-10-13-mNN-01` calls cost $12.18 and 769 turns and turned 125 candidates into 106. The
morning kept 18 of the 106; 33 were duplicates and 55 not know-how or overtaken
(`plans/handoff-LAPPLY13.md:47-62` in the brain). 911 of the 1,020 marks were status kinds (agent-done 347,
board-reply 266, hotovo 93, …), and those are no longer tracked since #236. The OPEN queue on 13. 10. at
10:00 is 1,069 marks, all know-how kinds (nalez 397, khms-issue 163, rozhodnuti 130, mereni 126, chyba 87, …).

## 2 The design

### 2.1 Shape
`tools/permark.py` (nový) replaces the agent call of the MARK stage. The stage asks the ledger for ONE
queue file (`mark_ledger.py queue --out`, oldest first; the chunking stays out). It hands that file to
`permark.py` together with the stage deadline. Marks are handled **sequentially**, in queue order. For each
mark: prefetch (0 tokens) → one `claude -p` call → parse → the night's output file is rewritten atomically
(tmp + rename). After the deadline no new mark is started. The output has the same shape as today's chunk
output (candidates, then `## MARKS NOT CARDED`), so the existing `mark_commit` takes it from there, with two
additions (2.5).

**Why sequential.** The in-night duplicate rule needs each mark to see every decision made before it.
Sequential processing gives exactly that with no locking. Parallel lanes would leave a race between marks in
flight at the same moment. Throughput is enough: a one-shot sonnet call with about 10k tokens in and 1.6k
out took 12.1 s on average (10-13 triage judge: 42 calls in 509 s). At 12–20 s per mark, 90 minutes cover
270–450 marks, above the know-how inflow of 141–214 marks a day (10-11, 10-12). The backlog of 1,069 drains
in roughly 5–12 nights. Lanes are question Q1, to be decided only if the measured rate falls short.

### 2.2 The packet — prefetched per mark, 0 tokens
1. **The mark** — its queue line (`id | date time | kind | journal/<file>:<line> | text`).
2. **Journal context** — lines `line-6 … line+3` of `journal/<file>`, each clipped to 600 characters; the
   mark's own line is whole. Lines that are themselves MARKs are kept: they are the mark's neighbours.
3. **Nearest cards** — the active, non-fog cards (`nearest_cards.active_cards()`) from three sources,
   de-duplicated:
   - every `K-nnnnn` the mark text names (khms-issue and supersede marks almost always name their target);
   - the top 8 of the lexical channel (`khms_search.search`);
   - the top 8 of the hybrid channel (`nearest_cards.rank` with the dense probe of `open_dense`).

   Shown: the id, status and title, then the first 3 body lines (500 characters at most). Shown WHOLE
   (up to 4,000 characters each): the named cards (at most 3) and the top-1 card of each channel.
   Measured (this design, script over the night's output, base restricted to cards older than K-05817):
   for the 38 know-how marks the night closed as "already carded K-x", the card was in the union of the
   two top-5 lists plus the named ids for 32 (84 %). With top-8 it was 34 (89 %), and the hybrid top-5 alone
   held 23. For the 428 status-kind marks the numbers were 317 and 333. Prefetch time: 0.44 s per mark.
4. **Tonight's candidates** — from the night's own output file: the 3 candidates the lexical channel ranks
   closest to the mark text, plus the most recently written one. Each is shown whole with its label and
   `MARKS:` line, 8,000 characters in all. The dense index holds only approved cards, so this ranking is
   lexical only.
5. **Transcript excerpt** — only when the mark ends in `(sess <8 hex>)`: 147 of the 1,069 OPEN marks; 601
   carry `(agent <role> #N)` and 183 `(diblin #N)`, which name no transcript. The session file is
   `<KHMS_TRANSCRIPTS>/<project>/<8hex>*.jsonl`. It is digested once per night by
   `preprocess_transcripts.digest_file` (48 MB → 1.5 MB in under 1 s, measured on session 22dd48c9). The cut:
   - the mark's local journal time is converted to UTC; the digest uses UTC;
   - the excerpt keeps the USER and ASSISTANT records whose stamp falls in `[t − 20 min, t + 2 min]`;
     TOOL records are dropped;
   - each record keeps its continuation lines. A Discord message carries its text on the line AFTER the
     `<channel …>` tag, so a filter on `[ts]` lines alone loses the owner's words (measured);
   - only the last 6,000 characters are kept, the end nearest the mark;
   - a mark without a time (`--:--`) gets no excerpt.

   Probe: for `M-9ed6e598` (journal 2026-10-13.md:79, 09:55 local) the window 07:35–07:57 UTC holds the
   owner's 07:50 and 07:54 messages verbatim.

Typical packet: about 5–7k tokens. The rules go in `--system-prompt` (2.3), are about 6k tokens and are
the same for every call.

### 2.3 The call
`claude -p --model sonnet --output-format json --tools "" --system-prompt <rules> --setting-sources ""
--strict-mcp-config --no-session-persistence --effort high`, with the packet on stdin and a 180 s timeout.
This is the `khms_contradict.claude_cmd` argv (`tools/khms_contradict.py:1249`) plus `--effort high` (every
stage call is sonnet · high). The model is the `sonnet` alias (owner 12. 10.). The served model is read from
`modelUsage` and logged (#232).

**The rules** = `tools/prompts/nightly-marks.md` rewritten (one home, rendered by `khms_prompt.py` as
today):
- kept word for word:
  - the contract (`:25-51`): every id ends in exactly one place, verbatim quote for "already carded",
    not know-how per K-03929, "value lives in <file>";
  - the candidate format, header shape and quote rules (`:53-88`);
- `## Inputs` (`:14-23`) and `## How you WRITE` (`:90-98`) are replaced by "the packet below; answer with
  exactly one decision block";
- added:
  - the MERGE PRESSURE `RELATION:` grammar from `nightly-consolidate.md:63-112`, so the evaluator does the
    consolidation itself;
  - K-05297's three kinds of untrue as the routing of decision 5 (2.4);
  - one quote rule: an "already carded" quote may come only from card text shown in the packet;
- also inlined: spec §4–§5 (8.9 kB) and the tag registry (3.7 kB). The agent used to read them with tools,
  and a call without tools cannot.

### 2.4 The decisions — exactly one per call
The first line names the kind; the rest is today's format.

| # | kind | first line | what follows (existing format) |
|---|---|---|---|
| 1 | not carded | `DECISION: not-carded` | `- M-x :: not know-how (K-93929): <why>` or `… value lives in <file>` |
| 2 | new candidate | `DECISION: new` | one `### M1: …` candidate: header, body, `RELATION:` line, `MARKS: M-x`, `**LINKS:**` if any, `**QUOTES:**` (the mark text verbatim, `src=journal`) |
| 3 | belongs to tonight's candidate X | `DECISION: belongs <label>` | either nothing (X already holds the content), or the REVISED X in full, its `MARKS:` the union |
| 4 | already carded | `DECISION: carded K-x` | `- M-x :: already carded K-x: "<sentence copied verbatim from K-x as shown>"` |
| 5 | correct K-x | `DECISION: supersede K-x` / `contradict K-x` / `erratum K-x` | supersede, contradict: a candidate with `RELATION: supersedes K-x BECAUSE …` or `RELATION: contradicts K-x`. Erratum: `- M-x :: erratum proposed K-x: "<false sentence verbatim>" -> <corrected sentence> (source: <file or card>)` |

The three kinds of untrue (K-05297) decide how decision 5 is written:
- **supersede** — a belief or a mechanism was overtaken;
- **contradict** — two live cards disagree and the mark does not settle which one is right;
- **erratum** — K-x was a wrong record against a source that has not changed, or a dated value whose source
  moved in the repo.

An erratum is not a card, so it is written as a NOT CARDED line with the reason. The morning review applies
it with `khms_erratum.py` and accepts the line as the dismissal. `mark_ledger.not_carded_events`
(`tools/mark_ledger.py:1044`) already records any non-"already carded" verdict as a dismissal with its
reason, so no parser changes.

The driver handles the answers with 0 tokens:
- **(3)** with no revision appends `M-x` to X's `MARKS:` line. With a revision it replaces X only when the
  new `MARKS:` keeps every old id; otherwise it appends and keeps the old X.
- **(2), (3), (5)** get a `NEAREST:` line with the ids shown in the packet, so `verify_relations` judges a
  `RELATION: new` against exactly what the model saw.
- An answer with no parseable block for this id is **not written**. The mark stays OPEN, returns tomorrow
  (as today), and is counted `unparsed`.

### 2.5 What changes in the night driver
- MARK stage (`tools/nightly_distill.sh:953-1017`): one `ledger queue --out` file, one
  `permark.py --queue … --out … --deadline <MARK_DEADLINE>` run, then `mark_commit` once. `MARK_CHUNK` and
  `MARK_TIMEOUT` retire; `MARK_BUDGET_MIN` stays.
- `mark_commit` (`:959`) gets two additions:
  - the post-checks of `postprocess_batch` (`ledger attribute`, `verify_quotes`, `verify_relations`) run on
    the output BEFORE the split; the function is moved above the MARK stage;
  - `nightly_batch.py split-run --ready` (nový flag) writes each `cons-NN.md` AND its `cons-NN.out.md` in
    the same temp directory before the one rename.
- **The consolidate call for mark batches is gone without touching the consolidation loop.** A batch whose
  durable output exists is published without a model call (`:1385-1395`). The header gate `card_header.py`
  still runs in `publish_batch` (`:1324`).
- Kill safety stays per transaction:
  - the output file is rewritten atomically after every mark;
  - a resumed run with the same night key (`nightly_retry.sh`) skips the ids already in it;
  - a new night key leaves the file unused, and its marks, still OPEN, are evaluated again (it costs money,
    nothing is lost);
  - a usage-limit answer ends `permark.py` with its own exit code, which the driver turns into `limit_stop`
    as for the other stages.
- The sweep, its consolidation and the triage are not touched.

## 3 Decision formats — one worked example per kind (real marks of the 2026-10-13 night)
"Prefetch" below is the measured ranking of this design's retrieval on the mark text, over the base
restricted to cards older than K-05817 (the cards approved on the morning of 13. 10. excluded), i.e.
the base the night had.

**1 · not carded** — `M-5f26a072` (mereni, `journal/2026-10-01.md:636`): "mergeg1: live rear depth …
hz=8.585 … TF base_link->dumpee_camera_rear z=0.048 pitch=1.880deg … — exact match to robot_physical.yaml".
Prefetch hybrid top-3: K-04397, K-03284, K-03303 (the rear-pose fix cards). The night's verdict was right
and the evaluator returns the same:

    DECISION: not-carded
    - M-5f26a072 :: not know-how (K-93929): value lives in robot_physical.yaml (rear depth rate and base_link->dumpee_camera_rear z/pitch)

**2 · new candidate** — `M-47f767ab` (nalez, `journal/2026-10-01.md:356`, agent investigator #83): the
anchor slope is pinned at +17.3 deg with gain 0.045. Prefetch: K-03429 lexical #1, K-03153 #2, K-03446,
K-02705, K-04354. None holds the slope numbers (the morning kept the night's candidate built from this mark,
M5001):

    DECISION: new
    ### M1: Height-anchor slope input pinned at +17.3 deg, gain 0.045 against true pitch — the carrier of the #83 height drift
    type: problem→solution
    level: observation
    tags: [nav, perception, imu]
    SYMPTOM: … CAUSE: ground_height_node reads the IMU orientation without its validity flag; anchor slope constant +17.3 deg, gain 0.045 …
    RELATION: supports K-03429
    MARKS: M-47f767ab
    **QUOTES:**
    - src=journal :: anchor slope constant +17.3 deg, gain 0.045 vs true pitch over a 15 deg range
    NEAREST: K-03429, K-03153, K-03446, K-02705, K-04354, …   (written by the driver)

**3 · belongs to tonight's candidate** — `M-8ca66a42` (`journal/2026-10-01.md:362`, three minutes later).
It is the same line again, with one line number changed (`268` instead of `269`). The packet shows M1 above
as tonight's closest candidate, and it holds the content:

    DECISION: belongs M1

The driver appends `M-8ca66a42` to M1's `MARKS:` line. The night put the two in one candidate as well. A
mark that adds a number would instead return the revised M1 whole, with `MARKS: M-47f767ab M-8ca66a42`.

**4 · already carded** — `M-244c31e8` (nalez, `journal/2026-10-01.md:412`): the second sim_clock cause.
Prefetch: K-05448 lexical #1 and hybrid #1, shown whole:

    DECISION: carded K-05448
    - M-244c31e8 :: already carded K-05448: "second independent sim_clock cause: fixture /clock advanced per timer callback, rcl skips late periods"

The quote passes `mark_ledger.quote_in_card` (checked: `True`).

**5a · supersede** — `M-7c11dd37` (owner-request→hotovo, `journal/2026-10-02.md:189`): "33 superseded karet
bez superseded_by → 0 …". K-03328 had measured that backlog on 2026-09-21. The correct output, which the
morning approved as K-05875, is:

    DECISION: supersede K-03328
    ### M1: owner request — superseded cards must point to their successors; the 33-card backlog closed to 0 on 2026-10-02
    … RELATION: supersedes K-03328 BECAUSE the backlog of 33 superseded cards without superseded_by that K-03328 measured on 2026-09-21 was closed to 0 on 2026-10-02
    MARKS: M-7c11dd37

**Measured limit:** K-03328 ranks below 20 in both channels for this Czech mark text. The night found it
only through the NEAREST of the candidate's ENGLISH text (#2), which is the K-01867 language gap. The
evaluator would therefore write a new candidate with `RELATION: supports K-03685`, and the morning would add
the edge. The plan counts how often this happens (`unseen-nearest`, section 5). It is the first candidate for
a later fix, not part of step 1.

**5b · erratum** (K-05297, wrong record) — `M-ac7f6f8e` (`journal/2026-09-29.md:245`): "triáž 30 řádků:
2 aplikovány, 28 zamítnuto". Prefetch hybrid #1 K-04220 and #2 K-03184 both say "3/30 applied". The source
(`memory/inbox/2026-09-29-triage.applied.md:3`) has not changed since, so this is a wrong record and needs an
erratum, not a new card:

    DECISION: erratum K-04220
    - M-ac7f6f8e :: erratum proposed K-04220: "the REV0929 triage sweep (2026-09-29-triage.md) applied at only 3/30 rows (10%)" -> 2 of 30 rows were applied; 3/30 was the judge's proposal (source: memory/inbox/2026-09-29-triage.applied.md:3)

This is exactly what the coordinator applied this morning (TS-00512 on K-04220, TS-00513 on K-03184). The
mark itself is of a status kind (`hotovo`) that #236 no longer tracks; today the same fact arrives as a
khms-issue mark (`journal/2026-10-13.md:50`), which names both cards, so they enter the packet as named ids.

## 4 Left out (Occam) — on purpose
- **Parallel lanes.** They are not needed while 270–450 marks a night exceed the inflow (Q1).
- **Transcripts for `(agent <role> #N)` and `(diblin #N)` marks.** The line names no transcript; resolving it
  through the subagent headers (Issue/Role) is more code than the 784 marks are likely to need.
- **A second call when the candidate's English NEAREST shows a card the evaluator never saw** (5a). It is
  only counted.
- **Earlier nights' failed verdicts for the same mark** (an unproven quote). The ledger keeps them; the
  evaluator starts fresh.
- **Unreviewed inbox units of earlier nights as "tonight's candidates".** The morning review runs daily and
  the ledger already keeps a proposed mark out of the queue.
- **Any new gate.** The quote check, `unparsed` and `unseen-nearest` are logged numbers; nothing is refused
  on them. `verify_relations` and `card_header` stay the only gates, as today.
- **Lower effort or a local model.** `--effort high`, the sonnet alias.

## 5 Measurement plan — one night against 2026-10-13
The night after the merge. The same 90-minute budget. The queue is know-how kinds only, while the 10-13 queue
was 89 % status kinds. Per-mark rates are therefore compared overall AND on the 109 know-how-kind marks of
10-13 (61 → candidates, 38 carded, 3 duplicate, 7 not know-how).

| number | 2026-10-13 (source) | measurement night (exact line) |
|---|---|---|
| $ per 60 marks | $1.27 MARK + $0.76 m-consolidate = $2.03 (`marks-N served-by: … $X` and `consolidate-2026-10-13-mNN-01 served-by:` lines of `nightly.log`) | `=== marks === permark: <N> mark(s) in <M> min, $<X> (CLI total_cost_usd), <T> turns, <C> candidate(s)` × 60/N |
| turns per 60 marks | 67 (1,141 / 17) | same line, T × 60/N; expected 60 |
| minutes per 60 marks | 5.4 (92.6 / 17) | same line, M × 60/N |
| m-consolidate calls | 16, $12.18, 769 turns | count of `consolidate-<night>-mNN-NN served-by:` lines: must be 0 |
| candidates per mark | 0.123 (125 / 1,020); 0.56 of know-how marks went into a candidate | the stage line, C/N; per kind from `nightly-<night>-permark.jsonl` |
| decision mix | 459 carded · 304 not know-how · 39 dup · 125 cand (`marks-N committed:` lines) | `permark: … decisions new=… belongs=… carded=… notcarded=… supersede=… contradict=… erratum=… unparsed=…` (every 60 marks) |
| unproven "already carded" quotes | 8 of 459 (`plans/handoff-LREV13B.md:24`) | `permark: … quotes-unproven=<n>` (`quote_in_card` run per decision, logged only) |
| supersede targets the packet never showed | not measured | `permark: … unseen-nearest=<n>` (a NEW candidate whose own NEAREST top-3 has an id the packet lacked) |
| **morning drop rate of mark-born candidates** | 88 of 106 dropped (83 %), 18 kept (`plans/handoff-LAPPLY13.md:47-62`) | the per-unit table of the morning's LAPPLY handoff (`m*-bNN \| in \| kept \| dropped`), checked by a count: `### ` headings in `memory/inbox/<night>-m01-b*.md` vs approved cards whose `marks:` hold those units' ids |
| morning review cost | LREV13A+B, the morning's librarian packages (journal 2026-10-13 line 77) | the same packages' token tables |

Every per-mark call also writes one row to `memory/inbox/.staging/nightly-<night>-permark.jsonl` (nový):
id, kind, decision, wall seconds, usage (in, out, cache read/write), `total_cost_usd`, served model.

Success means all three of these, and no laxer bar afterwards:
- the morning drop rate of mark-born candidates is clearly below 83 %;
- unparsed marks are under 2 %;
- $ per 60 marks is no more than $2.03.

If $ per mark comes out higher but the drop rate falls, the owner decides (Q2).

## 6 Cost
- **Per mark** (estimate). Basis: `tok_account` prices × 1.36, the measured ratio of CLI `total_cost_usd` to
  `tok_account` for the 10-13 judge call (K-05676 direction). The log reports CLI dollars, so the comparison
  stays in one currency.

  | decision | tokens | cost per mark |
  |---|---|---|
  | short (not carded / carded / belongs) | ~5k fresh in, 6k cached system prompt, 0.6k out | ≈ $0.026 |
  | full candidate | ~6.5k fresh in, 6k cached, 1.6k out | ≈ $0.045 |
  | worst, no cache hit | 12k in, 2k out | ≈ $0.068 |

  For a know-how queue (about 45 % full candidates) this is ≈ $0.035 per mark, ≈ $2.1 per 60 marks, range
  $1.6–4.1. That is about today's $2.03. **The step does not promise cheaper marks.** It buys:
  - a bounded call (1 turn instead of 44–103);
  - the right cards in front of the model (84–89 % of the cards the night carded to are in the packet);
  - no consolidate call;
  - fewer drops in the morning.

  The morning is where the money went (its librarians re-ran the night's search).
- **Per night:** 270–450 marks in 90 minutes ≈ $9.5–16 for the MARK stage, against $33.85 on 10-13
  (MARK $21.67 + m-consolidate $12.18). That is cheaper per night only because the budget caps the count.
- **To build:** one opus programmer package. Median opus package $5.86 (K-03590), estimate $6–12. A
  smoke run on 20 real queue marks before the night costs ≈ $0.7–1.4.

## 7 Step 2 (designed and costed here, NOT built now): the transcript sweep becomes a generator for the same evaluator
Owner, 13. 10. 08:07. Today the sweep reads the transcripts and the git log, writes full candidate cards
(31 parts) and has them consolidated (25 calls). In step 2 the sweep becomes a step BEFORE step 1. It only
writes candidate MARK lines, which are sorted by time together with the journal MARKs and go through the one
evaluator. The sweep's consolidation disappears; there is then one evaluator and one consolidation path.

**What a generated line must carry** (file `memory/ledger/generated/<date>.md` (nový). The journal stays the
record people and agents write, and K-05679 keeps the journal MARKs with the record stage):

    - <UTC ISO time> MARK[<know-how kind>] <one-line finding> (src=digest sess <8 hex> @<UTC HH:MM> | src=gitlog <8 hex sha>) :: "<verbatim source span, at most 300 characters>"

- **kind:** know-how kinds only (K-03929); the generator never writes status.
- **source label:** `src=digest` or `src=gitlog`, the labels `verify_quotes.py` already checks. The
  evaluator quotes the span with that label, never as `src=journal`.
- **verbatim span:** the quote anchor. Without it, the evaluator cannot write a grounded candidate.
- **session id + UTC time of the record**, or the commit sha: the prefetch then cuts the transcript excerpt
  (2.2 point 5), or `git show --stat <sha>`, around it.
- **id:** the ledger's own `M-` hash of kind|text. A re-run therefore never doubles a line.
- **no passage that already carries a journal MARK** (today's unmarked-source filter,
  `tools/nightly_distill.sh:541-559`).

What changes in the ledger: `mark_ledger.py` scans `generated/` beside `journal/` (`loc` =
`generated/<file>:<line>`).

**Cost** (estimate, 10-13 basis):
- the generator ≈ today's sweep parts, $9.39 for 26 parts ≈ $11 for 31; less, since it writes lines instead
  of cards;
- the sweep consolidation disappears: −$27.28 (25 calls, 1,410 turns);
- the evaluator ≈ $0.035 per generated line.

At today's sweep yield of 614 candidates that is ≈ $21, but also 2–3.4 hours sequentially, beyond the
90-minute budget. Step 2 is therefore only sound if the generator writes far fewer lines than today's 614
candidates. The morning kept 49 of the 184 sweep-born candidates that survived consolidation, so a
know-how-only generator should land in the low hundreds. Net, the night comes out about $6–16 cheaper.

**Its own measurement:**
- generated lines per night;
- evaluator candidates per generated line;
- minutes for the combined queue;
- the morning drop rate of sweep-born candidates (10-13: 135 of 184 dropped, 73 %, after a consolidation that
  had already cut 614 to 184);
- $ against the sweep + sweep-consolidation total of the same night.

## 8 Implementation checklist (each step with its acceptance test)
1. `tools/permark.py` (nový) covers the prefetch (2.2), the call (2.3), the parse and driver rules (2.4),
   the atomic output, resume by id, `--deadline`, the per-mark JSONL and the `permark:` log lines (5).
   Acceptance: `tests/test_permark.py` (nový) with the `CLAUDE_RUNNER`-style stub, one case each:
   - every decision kind parses into today's format;
   - every queued id ends in exactly one place, or stays out as `unparsed`;
   - `belongs` with a revision that drops an old id falls back to append;
   - a resumed run skips decided ids; no mark starts after the deadline;
   - the argv carries `--tools ""` and no `Read`, `Grep` or `Bash`;
   - the packet holds the named `K-` ids, the union of the two top-8 lists, and the transcript window
     converted to UTC with its continuation lines.
2. `tools/prompts/nightly-marks.md` rewritten per 2.3. Acceptance: it renders through `khms_prompt.py`, and
   `git diff` leaves the contract and the candidate format (`:25-88` today) unchanged except the inputs
   wording.
3. `nightly_batch.py split-run --ready`; `mark_commit` runs the post-checks before the split; the MARK stage
   calls `permark.py` once. Acceptance:
   - `tests/test_mark_phase_first.py`, `tests/test_night_pipeline.py` and `tests/test_night_finishes.py`
     are green;
   - a new case shows a mark batch published from its `cons-NN.out.md` with no consolidate call.
4. Smoke run on 20 real OPEN marks into a scratch output, nothing committed. Acceptance: 20 of 20 ids
   accounted or `unparsed` ≤ 1, `quotes-unproven` 0, cost in the JSONL. Also, by hand, the owner's
   2026-10-13 marks get their Discord words in the packet.
5. One measurement night, then the section-5 table filled from the named lines and posted to #237.

## 9 KHMS projito: K-05675, K-05679, K-03473, K-04320, K-03313, K-05683 (opened whole); K-05076, K-05034, K-03319, K-05035, K-04317, K-05676, K-05331, K-05393, K-01867, K-02597, K-05166, K-04350, K-03590 (recall summaries); nekonzistence: none in the base. The brief has two premises that do not hold:
- "from m-units 46 candidates in": `plans/handoff-LREV13B.md:16` gives 106 candidates in the 16 m-units, 18
  kept (`plans/handoff-LAPPLY13.md:47-62`);
- "111 of 967 OPEN marks carry a session or agent id": 147 of 1,069 carry `(sess …)` today. The 601
  `(agent <role> #N)` and 183 `(diblin #N)` marks carry a role and an issue, not an id.

One related observation: of the 38 know-how marks the night closed as "already carded", 8 point to cards
that are not active today. Whether they were active at 03:40 was not checked, so this is not an
inconsistency claim.

## 10 Questions for the owner (each with a recommendation)
1. **Throughput if the night is slow.** Recommendation: stay sequential. If the measured night covers fewer
   than about 250 marks in 90 minutes (below the inflow), raise `MARK_BUDGET_MIN` to 150 minutes rather than
   adding parallel lanes. Lanes reopen the in-night duplicate race.
2. **What counts as success if dollars per mark rise.** Recommendation: the morning drop rate of mark-born
   candidates is the target. Accept up to about $3 per 60 marks if it falls clearly below 83 %, because the
   morning review cost about twice the night's mark work on 13. 10.
3. **When step 2 is built.** Recommendation: only after step 1's measurement night, and only if the drop
   rate fell. Step 2 then goes through its own design round with the generator's line count as its first
   number.

# Historical triage experiments

These tools read the configured KHMS base and retained server versions. Missing historical
witnesses are UNVERIFIABLE (exit 3); current cards are never substituted for prior evidence.

- `eval_triage_selector.py` evaluates review rows; `eval_triage_judge.py` uses the same reader.
- `judge_exp.py verify` validates frozen packets and independent server-version anchors.
- `judge_exp.py build` refuses new historical graphs without a contemporaneous version inventory.
  `run` requires verified anchors; `--dry` renders existing packets without model calls.

`KHMS_JUDGE_EXPERIMENT_CONFIG` selects an optional JSON file outside the code tree. Keys:
`out`, `seed`, `labellers` (two names), `cited_docs`, `prior_caches`, `adjudication_format`,
`decision_pattern`. Relative paths resolve under `KHMS_ROOT`. Keep deployment names, evidence
paths and historical answer caches there. Existing pinned experiment evidence is never deleted.

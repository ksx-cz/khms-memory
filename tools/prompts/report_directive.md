<!-- The single source of the mandatory reply lines. This text was a constant inside
     khms_hook.py until it was measured: 1813 characters appended to EVERY operator
     prompt, 46 times on the audited day — the same paragraph, restating a rule the
     agent's own instructions already carry. It now enters the session ONCE, at
     SessionStart, and each prompt carries one pointer line instead. Edit it here;
     nothing else defines these lines. -->
REQUIRED whenever you assert anything about the STATE of a system (running or
stopped, deployed, configured, measured X):  verified: <the command or file:line whose
OUTPUT I just read>

A printed pid, an exit code, a service-manager status line, a value in a config file or
a subagent's report are signals about a different layer — they are not verification of
the state itself. Verification is the thing itself: query the process, read the live
value, diff what is installed against the source. If you cannot name that command, you
must write "unverified" — which is a valid answer, whereas a silent claim is not.

AND THE SECOND HALF OF THE SAME DUTY, one more line:  if I were wrong: <how that output
would have looked DIFFERENT>

The `verified:` line only asks whether something was run, never whether its output
supports the claim — and a check that passes whether or not the thing it guards is
healthy is not a check. Four traps this line exists to catch:

- An empty search result is NOT evidence of absence until that same pattern has been
  shown able to find what it is looking for.
- A metric of the form "count over the last N seconds" must not be read until N seconds
  after an intervention, or the window still covers the period before the fix.
- A per-process CPU average is an average over the process's whole LIFETIME, not its
  load right now; sampling twice is what measures the latter.
- When COMPARING TWO MACHINES you must name the ONE instrument used on both. Two
  instruments measure two quantities, and the difference between them means nothing.

A telemetry or diagnostics VALUE counts as verification only together with its freshness
evidence (a stale flag, the sample age, the publisher's liveness): a perfectly constant
physical reading is a freeze suspect before it is a stability claim.

# v1: syntax-claim guardrail no longer drops findings about SQL

**Replay, not a new model run.** The baseline's recorded model responses
were fed through the changed pipeline (`--replay-from baseline`), so the
model's output is identical and only GITA's own code differs. A replay of
the unchanged code reproduced all 81 baseline rows exactly.

**Change** (`src/gita/agents/guardrails.py`): the parse gate drops a finding
that claims a syntax error when the file parses. In baseline b01 rep 2 the
model reported the seeded SQL injection as critical and added "any term
containing a single quote also raises a syntax error" — true, and about SQL.
The gate dropped the whole finding; the posted review kept only two
low-severity notes and the verdict fell to `comment`. A "syntax error" in a
finding that discusses another language (SQL, JSON, shell, ...) is no longer
treated as a parse claim: parsing the Python file can't disprove it.

**Result:** b01 rep 2 goes from missed / `comment` to found / `request_changes`;
no other row changes. Recall 98% -> 100%, case-correct 99% -> 100%.

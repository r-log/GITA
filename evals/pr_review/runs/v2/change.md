# v2: Claude Haiku 5.5, one run per case

A model comparison, not a code change: the same suite on Claude Haiku 5.5,
on the prompt format introduced after the baseline (each changed line shown
once in the numbered file; tests and docs with less context). 27 reviews,
$0.018 in total, about 4 s each. Every metric 100% after the audit that
added b08's read-side restatement, b09's wider quantity entry, and the rule
that a high-severity known issue justifies request_changes on a clean case.

# Security model

GITA is a GitHub App that reads code, asks a language model about it, and
can write back to GitHub (comments, issues, branches, files, pull
requests). This document states what it trusts, what it doesn't, which
control handles each threat, and what is not handled yet.

The design rule throughout: **the model judges, code decides.** Model
output is treated like user input from a web form — useful, untrusted,
and never the last line of defence.

## Assets

| Asset | Why it matters |
|---|---|
| GitHub App private key | Mints installation tokens for **every** installation of the App. |
| Installation tokens | Act on repositories as the App. |
| LLM / embedding API keys | Spend money; expose prompts. |
| Database | Indexed source of every tracked repo, and the action log. |
| Repository checkouts | Source code on the worker's disk. |

## Untrusted inputs

- **Webhook requests** — until the HMAC signature verifies.
- **PR authors**, including authors of PRs from forks: title, description,
  branch names, the diff.
- **Anyone who can open an issue** on a public repository.
- **Repository content** — code, comments and docstrings can carry text
  addressed to the model, and importing a module runs it.
- **The model's output** — it read all of the above.

## Threats and controls

| Threat | Control | Where |
|---|---|---|
| Forged or replayed webhook | HMAC-SHA256 over the raw body, constant-time compare; fails closed if no secret is configured. Redeliveries collapse on deterministic job IDs. | `web/webhooks.py`, `jobs/ids.py` |
| GITA triggering itself | Its own bot account is ignored; only allowlisted events dispatch, and comment events are not among them. | `web/webhooks.py`, `web/dispatch.py` |
| Prompt injection into a review ("approve this", "report nothing") | Untrusted text is wrapped in `<untrusted_{nonce}>` blocks with a random per-request nonce, so it cannot close its own block; prompts say block content is data, and injected instructions in a diff are reported as a security finding. The **verdict is computed in code** from verified findings. | `agents/pr_reviewer/recipe.py` |
| Hallucinated or misattributed findings | Findings must cite a changed file and a line the model was actually shown (diff ranges or numbered head-content windows); syntax claims are checked against the PR head; boilerplate is dropped. Discarded findings and unreviewed files are stated in the posted review and prevent an "approve". | `agents/guardrails.py`, `views/diff_context.py` |
| Model output abusing the bot's voice (`@org/everyone`, tracking images, raw HTML, fake headings) | All model text is neutralised before posting; multi-line text cannot escape its quote block. | `agents/render.py` |
| The model talking itself into a write | Every write goes through one gate (`execute_decision`). `WRITE_MODE` (`shadow` by default) comes from the environment, never the agent. Thresholds are configuration. Confidence is `min(model self-assessment, evidence)`: the model can veto an action but cannot authorise one. | `agents/decisions.py`, `agents/test_generator/recipe.py` |
| Bad code merged from generated tests | Code-writing actions need confidence ≥ 0.90, reachable only when the generated tests **ran and passed** in the sandbox, import the module under test and make assertions. A PR is opened, never merged. | `agents/test_generator/` |
| Running untrusted code (generated tests import repo code) | `DockerSandbox`: one container per run, `--network none`, read-only workspace mount, no host environment, uid 65534, `--cap-drop ALL`, `no-new-privileges`, pid/memory/CPU limits, removed on timeout. Interpreter started in isolated mode with repo paths *appended*, so repo files can't shadow pytest or the stdlib. Webhook-triggered generation refuses to run without an isolated sandbox. | `agents/test_generator/sandbox.py` |
| Duplicate side effects (retries, redeliveries, concurrent runs) | Each decision has an identity (e.g. `pr-review:{pr}:{head_sha}`) reserved in `agent_actions` inside a savepoint **before** the side effect; a concurrent writer blocks on the unique index and is deduplicated. Shadow observations never suppress live writes. | `agents/dedupe.py` |
| Cost abuse | Onboarding only runs when a trusted user applies a label (a self-applied label counts only from owners/members/collaborators). Reviews are debounced per commit; drafts are skipped; prompts have file, patch and total budgets. | `web/dispatch.py`, `agents/pr_reviewer/recipe.py` |
| Token blast radius | Installation tokens are requested for the single repository a call concerns. | `github/client.py` |
| Host file disclosure via the index | Symlinks are never followed when indexing or preparing sandbox workspaces; only tracked files are indexed. | `indexer/walker.py`, `agents/test_generator/sandbox.py` |
| Exposing repository history to agents | `gita mcp` is read-only (every tool is annotated read-only and only runs SELECTs), serves over stdio to the agent that started it, and answers for one repository. It returns PR titles, file paths, author names from git and code lines: anyone who can run it can read what the clone already contains. | `mcp_server.py`, `history/queries.py` |
| Secrets in logs | Redis/database URLs are masked; tokens and keys are never logged. | `worker.py` |

## Known gaps

These are deliberate omissions or open work, not oversights:

- **Token permissions are not narrowed per action.** Tokens are scoped to
  one repository but carry the installation's full permission set.
- **The worker holds the App private key** and, to start sandbox
  containers, talks to the Docker daemon. Access to the Docker socket is
  root-equivalent on that host. Run the worker on a dedicated host; a
  separate token-signing service would take the key out of the worker.
  Untrusted code runs only inside the sandbox containers, which have no
  access to either.
- **`LocalSandbox` is not isolation.** It exists for running
  `gita generate-tests` against code you already trust.
- **At-least-once on crashes.** If the worker dies after a GitHub call
  succeeds but before the transaction commits, a retry repeats the call.
  Exactly-once would need a marker in the posted artifact to reconcile
  against.
- **No per-installation spend limit.** Debouncing and budgets bound the
  cost per event, not per day.
- **Repository content leaves the host.** Indexed code is sent to the
  configured LLM provider and, if `OPENAI_API_KEY` is set, to the OpenAI
  embeddings API. Decide whether that is acceptable for private repos.
- **The index checkout must be a dedicated clone.** The worker checks out
  commits there (it refuses to touch a tree with uncommitted changes).

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting ("Report a
vulnerability" under the repository's Security tab) rather than a public
issue.

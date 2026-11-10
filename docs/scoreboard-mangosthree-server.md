# How often merged PRs needed a later fix: mangosthree-server

2026-06-02 to 2026-11-10: 725 merged PRs, 68 of them fixes.

- **Later needed a fix:** 2.6% (19/725)
- **Fixes traced to a PR:** 20; fixes of code older than any tracked PR: 39
- **Days until fixed:** median 2.0, 90th percentile 48
- **Fixes that fixed a fix:** 3

| Kind of PR | Later needed a fix |
|---|---|
| chore | 0.0% (0/38) |
| docs | 0.0% (0/12) |
| feature | 1.9% (7/367) |
| fix | 4.4% (3/68) |
| other | 3.3% (4/121) |
| refactor | 4.3% (5/116) |
| test | 0.0% (0/3) |

| Size (added lines) | Later needed a fix |
|---|---|
| <50 lines | 0.0% (0/123) |
| 50-199 | 1.8% (3/165) |
| 200-499 | 3.5% (7/201) |
| 500+ | 3.8% (9/236) |

| Area | Defects traced there |
|---|---|
| `src/game` | 38 |
| `src/realmd` | 2 |
| `src/proto` | 1 |

| Month | Later needed a fix |
|---|---|
| 2026-06 | 50.0% (1/2) |
| 2026-08 | 4.5% (2/44) |
| 2026-09 | 1.5% (2/133) |
| 2026-10 | 2.6% (12/457) |
| 2026-11 | 2.2% (2/89) |

*Later needed a fix:* a later fix PR changed lines this PR wrote, and this PR holds at least 50% of the fix's traced lines.

- Line tracing is evidence, not proof (a refactor that moved buggy code is credited with it).
- Only fixes made as PRs with a fix kind are seen.
- Recent months look better than they are: their bugs may not have been found yet.


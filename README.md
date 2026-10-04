# gate

Part of **quirq infra** ("qq"), quirq-ai's CI/CD system for repos in any language. This repo
decides what must pass before a change lands: the required checks, computed from
[infra-config](https://github.com/quirq-ai/infra-config) and each repo's manifest, and the merge
queue that verifies the exact merge result before it reaches `main`.

**Chromium counterpart:** LUCI CV (Change Verifier) and `commit-queue.cfg`. As there, the gate is
deterministic config, not judgment: no agent can override it, and every required check also runs
on the merge result (GitHub `merge_group`), not only on the proposed change.

## v0 scope

| Item | What | Needs |
| --- | --- | --- |
| V0-GAT-01 | Required checks computed from `gate.toml`, `pipelines.toml` and manifests | V0-CFG-02 |
| V0-ORG-03 | Merge queue and rulesets as code, with an apply script suraj runs | V0-GAT-01 |
| V0-GAT-02 | Agnosticism guard: no core file names a language, build tool or deploy target | V0-GAT-01 |
| V0-GAT-03 | Verification-surface rule: tests, quarantine and `infra/` need a non-author owner | V0-GAT-01, V0-ORG-02 |
| V0-GAT-04 | Gate timing (queue entry to verdict, p50 and p90 per repo) in scorecard v0 | V0-TST-02 |

Out of scope for v0: result reuse, admission-bar enforcement and tree status (v1); cross-repo
gating and a queue for Launchpad (v2).

## v0 status

| Item | PR | State |
| --- | --- | --- |
| bootstrap | #1 | in review |
| V0-GAT-01 | | not started |
| V0-ORG-03 | | waits on V0-GAT-01 |
| V0-GAT-02 | | waits on V0-GAT-01 |
| V0-GAT-03 | | waits on V0-GAT-01, V0-ORG-02 (suraj's owners) |
| V0-GAT-04 | | waits on V0-TST-02 (test-pipelines) |

## Known conflict

suraj cannot approve PRs opened under his own account, so an owner-review rule needs a second
human owner or a bot identity. TODO(suraj): pick one; the gate does not work around it.

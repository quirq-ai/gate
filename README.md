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

## How it works (V0-GAT-01)

- Policy comes only from [infra-config](https://github.com/quirq-ai/infra-config), read at the
  commit in `pins.toml` through infra-config's own `qqcfg` (`qqcfg validate` must pass first, or the
  gate refuses to compute anything). Manifests (`infra/repo.toml`) are read only through
  [sync](https://github.com/quirq-ai/sync) (`qqsync`), also by pinned commit.
- `gate.toml [merge_queue] required = "blocking-builders"`: every builder in `pipelines.toml` with
  `blocking = true` for a repo is a required check, named after the builder.
- Each required check must run on the change and on the exact merge result (triggers `change` and
  `queue`, GitHub `pull_request` and `merge_group`). A blocking builder without `queue` is an error.
- A repo with no blocking builder is refused as ungated. With a manifest, every target's kind must
  be covered by a blocking builder, so no target lands unverified.
- Backend code sits in `qqgate/backends/<backend>.py`, picked by the `backend` field. `github`
  checks that infra-config generates a workflow whose job is each required check on both events,
  emits the ruleset's `required_status_checks` rule (checks must come from the GitHub Actions app),
  and reads a commit's check runs. `launchpad` is one new module.
- A verdict passes only when every required check reported `success`. Missing, running, skipped
  and neutral checks are refusals. Exit codes: 0 pass, 1 refused, 2 the gate could not decide,
  3 the repo is not in repos.toml (`required`, `rule` and `verdict`; with `--json`, stdout is `{"repo": ..., "onboarded": false}`).

```sh
qqgate required --config ../infra-config --repo xo-space [--manifest infra/repo.toml]
qqgate rule     --config ../infra-config --repo xo-space          # ruleset rule JSON (V0-ORG-03 applies it)
qqgate verdict  --config ../infra-config --repo xo-space --sha <commit>   # or --observed checks.json
```

Today: `xo-space` requires `xo-space-presubmit`, `innernet` requires `innernet-presubmit`.
xo-space's hand-written `tests` check is not required: the generated presubmit runs everything it
ran, and V0-ONB-01 deletes `tests.yml`. `transitional_checks` in `settings/github.toml` can still add
a hand-written check after the generated ones.

"A red PR is refused" has two halves. CI here proves the gate's half: a red check on either repo
gives a refusal. The live half needs the generated workflows delivered (xo-space #211, innernet
#37), the rulesets applied (V0-ORG-03, suraj) and the manifests (V0-ONB-01/02).

## Merge queue and rulesets (V0-ORG-03)

`settings/github.toml` is the merge queue and rulesets as code, for all thirteen infra repos and
both product repos. `qqgate settings plan` prints them, `verify` says which repos are safe to switch
on today (every required check must run on `pull_request` and `merge_group` there), and `apply`
creates or updates them by name for ready repos only. An org admin runs `apply`; see
[docs/apply-settings.md](docs/apply-settings.md).

## Agnosticism guard (V0-GAT-02)

`qqgate guard --repo <core repo> [ROOT]` fails if shipped code in a core repo (depot, sync, gate,
test-pipelines, gardener, release; plan §5.1) names a language, build tool or deploy target. Python
files are read with `ast` (identifiers, imports, strings) and `tokenize` (comments); other files are
grepped. Tests, docs and CI are not core code. The terms and the reviewed per-file exceptions (for
example depot's launcher, which starts qq's own Python) are in `guard/terms.toml`, a policy file;
there is no inline pragma. Registered media types (`application/vnd.docker...`) are formats, not
targets, and are skipped. Each core repo adds
`qqgate guard --repo <name> .` to its presubmit; this repo's `settings-drift` job sweeps them all.

## Gate timing (V0-GAT-04)

Scorecard v0 (test-pipelines) reports gate time per repo as p50 and p90 of queue entry to verdict,
from each gate run's `Run.queued_at`. The merge_group payload carries no queue-entry time, so a gate
job runs `quirq-ai/gate/timing@<commit>` before the result sink: it reads the PR from the queue ref,
takes the newest `added_to_merge_queue` event on its timeline at or before the group was built, and
exports `QQ_QUEUED_AT` (RFC 3339 UTC) for the sink. If the timeline cannot be read it exports nothing
(the merge-group commit's time would understate the wait without anyone seeing it) and warns. It
never fails the job, and it installs nothing: it runs gate's own source on whatever `python3` is
first on PATH (3.11 or later; older warns and skips), isolated from site-packages, with the standard
library only. The job's token needs `pull-requests: read` (and
`contents: read`).

Scorecard caveats for v0: with several PRs in one group, the group is timed from the tip PR's queue
entry. Put the timing step and the sink only in the gate job that finishes last, so each gate run
is one sample.

## v0 status

| Item | PR | State |
| --- | --- | --- |
| bootstrap | #1 | merged |
| V0-GAT-01 | #2 | merged; live demo waits on V0-ORG-03, V0-ONB-01/02 |
| V0-ORG-03 | #3, #5, #9, #10 | merged; waits on suraj to apply ([docs/apply-settings.md](docs/apply-settings.md)). Pinned product presubmit org rulesets wait on infra-config dropping cancel-in-progress |
| V0-GAT-02 | #4 | merged |
| V0-GAT-03 | | waits on V0-ORG-02 (suraj's owners) |
| V0-GAT-04 | #6 | merged; the sink reads QQ_QUEUED_AT (test-pipelines 1e3ddb1). Numbers appear once infra-config adds the timing step and the queue runs |
| not-onboarded exit 3 (for depot) | #7 | merged |

## Known conflict

suraj cannot approve PRs opened under his own account, so an owner-review rule needs a second
human owner or a bot identity. TODO(suraj): pick one; the gate does not work around it.

## License

Apache License 2.0; see [LICENSE](LICENSE).

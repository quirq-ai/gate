# Applying the gate's settings (V0-ORG-03, admin: suraj)

The merge queue and rulesets are code in `settings/github.toml`; `qqgate settings` turns them into
GitHub rulesets. An agent cannot apply them (it would be changing its own gate); an org admin runs
one command, from the gate commit the coordinator names.

## The command

You need `git`, Python 3.11 or newer, and `gh` logged in as an org owner (`gh auth login`). Do not
export a token: qqgate asks `gh auth token` itself, after the step that runs infra-config's code
has exited.

```sh
git clone -q https://github.com/quirq-ai/gate qq-gate && cd qq-gate && git checkout -q <COMMIT> && scripts/apply.sh
```

[`scripts/apply.sh`](../scripts/apply.sh) then:

1. Checks the tools, that `gh` is logged in and that the gate checkout is clean.
2. Makes `.venv` from `apply-requirements.txt` (hashed, fully pinned, wheels only).
3. Clones infra-config at the commit `pins.toml` names, and every repo in `settings/github.toml`
   fresh into `.qq/repos` (re-running clones again, so nothing is stale).
4. Runs `settings verify` (which repos are ready) and a dry run (GETs only), and prints them.
5. Asks you to type `yes` before writing. It asks separately, first, if the dry run printed a
   `WARNING` (other protection already on a repo, see below) or a ruleset marked `differs` (one of
   ours that someone changed on GitHub; writing replaces it, so a bypass added in the UI is
   removed).
6. Writes the repo rulesets.
7. Only if an org ruleset is enabled in `settings/github.toml` (today: the two pinned product
   presubmits): asks before running `gh auth refresh -h github.com -s admin:org`, shows the org dry run, asks
   again, writes, and on exit runs `gh auth refresh -h github.com --remove-scopes admin:org` (unless
   gh already had that scope before). The org run re-plans the repo rulesets too; they show as
   `unchanged` and are not written again.

Nothing is written before a `yes`. Exit 0 means everything is applied; 1 means something was not
ready, skipped or refused, or you answered something other than `yes` (the output says what; fix it
and run the command again); 2 means an error stopped it. The script refuses to start while
`GH_TOKEN` or `GITHUB_TOKEN` is set, since the org step refreshes gh's stored login. Re-running is always safe.

The script calls `qqgate settings verify` and `qqgate settings apply` (`--yes`, `--accept-warnings`,
`--overwrite`, `--org`); each refuses on its own what the script asks about, so running them by hand
is no less safe.

## What a run does and does not do

- It only writes repos that are ready, and skips the rest with the reason.
- Every request goes to `https://api.github.com`. A redirect is refused rather than followed, so
  the token cannot reach another host (a renamed repo stops the run with an error instead).
- The plan (for `verify` as well as `apply`) is computed in a child process whose environment holds only `PATH`, locale and temp
  settings, with an empty `HOME`: no token, SSH agent, netrc, `gh` or git config. Only that child
  runs infra-config's code, and the token is read from `gh` after it has exited (a same-user process
  can read its parent's environment). If `QQ_GITHUB_TOKEN` is set anyway, qqgate uses it and warns.
  What makes the child's code trustworthy is the pinned infra-config commit (apply refuses any other
  commit, or local changes). The process holding your token reads `settings/github.toml` before the
  child starts (and refuses if it changes), takes only product repos' required check names and two
  gate.toml numbers from the child, checks that each name is a job in a workflow the pinned
  infra-config generates and that transitional checks come last, and builds every ruleset itself.
- Within one `qqgate settings apply`, every GET happens before the first write (the script's org
  run is a second invocation, after the repo writes). The dry run prints a `plan` line per ruleset (`create`,
  `update` with the fields that differ, or `unchanged`) and a `WARNING` for classic branch protection
  or other rulesets already on a repo (they stack with ours: remove them, or check their required
  checks run on `merge_group`) and for a repo with squash merging turned off.
- `--yes` alone refuses, writing nothing, while there are `WARNING` lines (add `--accept-warnings`)
  or rulesets that differ (add `--overwrite`).
- Each write is printed as soon as it succeeds. If one fails, the run stops with `FAILED`, says the
  lines above it that do not start with `plan` are already live, and lists what it did not attempt.
  Rulesets are created or updated by name, never deleted.
- It never touches rulesets or protection it did not create. A ruleset dropped from
  `settings/github.toml` stays live until an admin deletes it in Settings > Rules.

## What each ready repo gets

- `qq-main` on the default branch: pull request required, merge queue (squash, all-green grouping,
  verdict timeout = gate.toml's 40-minute admission limit), the required checks (each pinned to the
  GitHub Actions app), no force push, no deletion, and nobody on the bypass list. Together these
  refuse a direct push to `main`. toolchains also requires code-owner review, which takes effect for
  the paths its CODEOWNERS gives owners (none are named yet: ORG-02).
- `qq-release-refs-branches` and `qq-release-refs-tags`: `lkgr` and `channels/**` cannot be created,
  moved or deleted except by the release executor. Its identity is not decided yet, so today nobody
  can write them.
- `qq-reserved-tags`, in every repo: nobody may create, move or delete a tag named `main` (a tag
  of that name satisfies a workflow's `github.ref_name == 'main'` test).
- `qq-state-branches`, where `state_branches` names some (gardener `ledger` and `tree-status`,
  release `release-state`, perf `perf-data`): those branches cannot be deleted or force-pushed.
  Their bots still push to them normally. Later, once the release executor exists, `release-state`
  should also be writable only by it (TODO(suraj) in `settings/github.toml`).
- `qq-dependabot-branches`, in xo-space and innernet: only Dependabot may push to or force-push
  `dependabot/**`, so the commit rollers checked is the commit that lands. The bypass names the
  Dependabot app by id 29110, the id commonly given for it; it could not be checked from here. If a
  Dependabot update is refused, read the app id with `gh api /apps/dependabot --jq .id` and change
  `actor_id` in `settings/github.toml`.

## When `verify` refuses a repo

- Its checkout is not a fresh clone of the default branch's current head, or the repo has no
  commits (qq-main would refuse the push that creates `main`: push a first commit first).
- It has no required check (its queue would land anything).
- Its checkout was cloned from anywhere but `https://github.com/quirq-ai/<repo>`.
- A required check can skip, pass without checking, or never report: a job-level `if:` other than
  exactly `always()` (unless listed in `allow_conditional` with a reason); `needs:` without exactly
  `always()`; an `always()` job after `needs:` that never compares `needs.<job>.result` with
  `success`; a matrix job; a job that calls a reusable workflow; a job with `continue-on-error`; a
  job with an `environment:` (its approval or wait timer can hold the queue); a path-filtered
  workflow; a `pull_request` or `merge_group` branch filter (including `!` patterns and
  `branches-ignore`) that leaves out the default branch; `pull_request` types without `opened` and
  `synchronize`, or `merge_group` types without `checks_requested`; a name used by two jobs.

Status (from `settings verify` on fresh clones, 2026-10-04 14:05 UTC): every repo ready. `verify`
prints each repo's commit, so a repo that moves between the clone and the run shows as not ready;
run the command again.

## Org rulesets (`--org`, needs admin:org)

`[[org_workflows]]` in `settings/github.toml` lists org rulesets that run a workflow from another
repo on every PR and queue entry, so a PR cannot satisfy them with its own same-named job. Turning
one on or off is a reviewed change to that file, after which the same command applies it.

- `qq-xo-space-presubmit-pinned` and `qq-innernet-presubmit-pinned` (on): each product repo's presubmit,
  run from infra-config's `.github/workflows/qq-required-<repo>-presubmit.yml` at a pinned commit
  (`sha`), so neither a PR nor a dependency roll can change the workflow that judges it. rollers
  auto-lands only into a repo that has one. Pinned at infra-config `eaa2c88` (#19), whose files
  dropped `cancel-in-progress`; both pass the checks below on a fresh clone. The pin fixes the workflow file, not
  the code it runs: owner review of tests and scripts is V0-GAT-03 (waits on ORG-02 owners).
- `qq-toolchains-promotion-gate` (off): toolchains' `promotion-gate.yml`. It dropped
  `cancel-in-progress` (toolchains #12); it waits on `timeout-minutes` coming down from 45 to 40 or less.
- `qq-drift` (off): infra-config's `qq-drift.yml` in the product repos. Waits on that file only checking
  the default branch against a pinned config.

`verify` (from the fresh clone, at its `sha`) and `apply --org` (again, through the API) both read
each enabled entry's file and refuse it unless it runs on `merge_group` and a pull request event,
never cancels in progress, and every job has a `timeout-minutes` within gate.toml's 40-minute
admission limit (the queue drops an entry whose check has not reported by then). `apply --org` also
checks that a pinned entry's commit is on infra-config's `main`. A pinned entry's file may have no path filter, no job that can skip
(the only job `if:` allowed is the repository guard), and no job or step `continue-on-error`.

An org ruleset targets only repos that are ready in that run, and keeps targets an earlier run
added, so a run limited to some repos never drops the others. A pinned entry judges exactly one
repo; if its live ruleset names others, the run refuses until that is fixed in Settings > Rules.

How GitHub runs these (docs: "Available rules for rulesets", "Troubleshooting rules"): a workflow
in a public repo can run in any repo of the org; it runs on `pull_request` (opened, synchronize,
reopened) and `merge_group` and ignores the workflow's own `branches`, `paths` and `types` filters;
it does not run for a PR opened or updated with a workflow's `GITHUB_TOKEN` (qq's bots use their own
App tokens); PRs already open get it on their next push. TODO(suraj): confirm the org's plan offers
"Require workflows to pass before merging".

**If a required workflow blocks every PR** (it fails or never reports): an org owner opens
github.com/organizations/quirq-ai/settings/rules, opens that ruleset and sets Enforcement to
Disabled (or Evaluate, which reports without blocking). Then fix the file, re-pin, and run the
command again; it sets the ruleset back to active.

## Decisions for suraj (`TODO(suraj)` in `settings/github.toml`)

- Squash merges for every repo: confirmed 2026-10-04.
- Whether admins get a break-glass bypass on `main`. The default is none. An org owner can still
  disable or edit a ruleset in Settings > Rules (the next run reports that as `differs`).
- The release executor's identity (a GitHub App id) for `lkgr`, `channels/**` and `release-state`.
- Approvals: `required_approvals = 0` today, because every PR here comes from an agent account and
  you cannot approve your own PRs. Code-owner review on tests and `infra/` is V0-GAT-03.

Done-when check after applying: `git push origin HEAD:main` to any written repo is refused, and a PR
lands only through the merge queue.

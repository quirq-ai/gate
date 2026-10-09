# Applying the gate's settings (V0-ORG-03, admin: suraj)

The merge queue and rulesets are code in `settings/github.toml`; `qqgate settings` turns them into
GitHub rulesets. An agent cannot apply them (it would be changing its own gate); an org admin runs
one command, from the gate commit the coordinator names.

## The command

You need `git`, Python 3.11 or newer, and `gh` logged in as an org owner (`gh auth login`). Do not
export a token: qqgate asks `gh auth token` itself, after the step that runs infra-config's code
has exited.

```sh
( mkdir -p ~/qq-apply && cd ~/qq-apply && rm -rf qq-gate && git clone -q https://github.com/quirq-ai/gate qq-gate && cd ./qq-gate && git checkout -q <COMMIT> && scripts/apply.sh )
```

Paste it from any directory, as often as you like: it all runs in a subshell `( ... )`, so your
shell stays in the directory you were in, with no variable changed. The work happens in
`~/qq-apply`, and no leftover there can make it fail: each run deletes and clones `qq-gate` fresh
and builds its `.venv` from scratch, so nothing left in an earlier clone (an edited script, a
package in the venv) can run. The one file kept between runs is `~/qq-apply/.qq-gate-org-done`,
outside the clone.

**If anything was changed or deleted on GitHub** (a ruleset edited or removed in the UI, or one that
looks off): run `rm -f ~/qq-apply/.qq-gate-org-done`, then the command again. Without
that, a re-run of a commit whose org rulesets were already applied skips the org step and does not
look at them on GitHub. The repo rulesets are always checked.

[`scripts/apply.sh`](../scripts/apply.sh) then:

1. Checks the tools, that `gh` is logged in and that the gate checkout is clean.
2. Makes a new `.venv` (`--clear`) from `apply-requirements.txt` (hashed, fully pinned, wheels only).
3. Clones infra-config at the commit `pins.toml` names, and every repo in `settings/github.toml`
   fresh into `.qq/repos` (re-running clones again, so nothing is stale).
4. Runs `settings verify` (which repos are ready), cloning again any repo that moved since its
   clone, then a dry run (GETs only), and prints them.
5. Asks "Apply these N changes to GitHub? Type yes to apply, anything else cancels" before writing
   (only a literal `yes` goes on; any other answer writes nothing), and writes only what you saw:
   the dry run saves each change and WARNING it showed, and the write refuses if it would do anything else (a ruleset
   edited on GitHub meanwhile, a new WARNING, a repo that became ready). Then nothing is written
   and the dry run is shown again. A repo that moved meanwhile just drops out of the write and is
   offered again on its own. It asks separately, first, if the dry run printed a
   `WARNING` (other protection already on a repo, see below) or a ruleset marked `differs` (one of
   ours that is not what settings say: settings changed, or someone edited it on GitHub; writing
   replaces it, so a bypass added in the UI is removed).
6. Writes the repo rulesets and repo settings (`allow_auto_merge`, below). A repo that moved between its clone and the write is skipped by
   qqgate; the script clones it again and offers just that repo again (dry run and `yes`), up to
   three rounds, so you do not start over.
7. Only if an org ruleset is enabled in `settings/github.toml` (today none: quirq-ai is on GitHub
   Free, which has no org rulesets, so the run never asks for admin:org): asks before running `gh auth refresh -h github.com -s admin:org`, shows the org dry run, asks
   again, writes, and on exit runs `gh auth refresh -h github.com --remove-scopes admin:org` (unless
   gh already had that scope before; with the scope already there it does not ask gh at all). Before
   asking for the scope it checks that the org's plan can run them and that your gh can remove a
   scope again (`--remove-scopes`, newer than gh 2.27); if not, it skips the step and adds nothing.
   While a scope it added is still on gh, `~/qq-apply/.qq-gate-admin-org-added` exists, and the next
   run removes the scope first, or prints the one line that does. The
   org run re-plans the repo rulesets too; they show as `unchanged` and are not written again. Once
   the org rulesets of a commit are applied, the script records that commit in
   `~/qq-apply/.qq-gate-org-done`, and a re-run of the same commit skips the org step and its browser prompts,
   without checking the org rulesets for edits made on GitHub since (see above: delete the file to
   run it again).

Nothing is written before a `yes`. Exit 0 means everything is applied; 1 means something was not
ready, skipped or refused, or you answered something other than `yes` (the output says what; fix it
and run the command again); 2 means an error stopped it. The script refuses to start while
`GH_TOKEN` or `GITHUB_TOKEN` is set, since the org step refreshes gh's stored login. Re-running is always safe.

The script calls `qqgate settings verify` and `qqgate settings apply` (`--yes`, `--save-plan`, `--expect-plan`,
`--accept-warnings`, `--overwrite`, `--org`); each refuses on its own what the script asks about, so running them by hand
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
  refuse a direct push to `main`. toolchains also requires code-owner review: its CODEOWNERS (toolchains
  #14) names suraj for `/.github/`, `/tools/`, `/toolchains/`, `/toolchains.toml` and
  `/promoted.toml`, so a change there needs his approval once `qq-main` is applied. GitHub does
  not let anyone approve their own PR and nobody bypasses `qq-main`, so a PR suraj opens himself that
  touches those paths cannot merge until a second owner is named (README: TODO(suraj)). toolchains
  also requires `promotion-gate` besides `ci`, and its queue merges one PR per group.
- `qq-release-refs-branches` and `qq-release-refs-tags`: `lkgr` and `channels/**/*` cannot be created,
  moved or deleted except by the release executor, a dedicated GitHub App (quirq-release-executor).
  Its App ID goes in `[release_refs] bypass_integration_ids`, and it bypasses these rulesets only in
  `executor_repos` (release, innernet, xo-space, website), the repos the App is installed on. Every other
  repo keeps no bypass: GitHub may refuse an Integration bypass for an App that is not installed,
  and the dry run (GET only) could not show that before the write. The App ID is 5199903 (created
  2026-10-05).
- `qq-reserved-tags`, in every repo: nobody may create, move or delete a tag named `main` or like
  any repo's state branch (`ledger`, `perf-data`, `release-state`, `results`, `tree-status`). A tag
  named `main` satisfies a workflow's `github.ref_name == 'main'` test, and a tag wins over a branch
  of its name on a short-name `git fetch`. `lkgr` and `channels/**/*` tags are already locked to the
  release executor by `qq-release-refs-tags`.
- `qq-state-branches`, where `state_branches` names some (gardener `ledger` and `tree-status`,
  release `release-state`, perf `perf-data`, test-pipelines `results`): those branches cannot be deleted or force-pushed.
  Their bots still push to them normally, except release-state once `qq-release-state` (below) is on.
- `qq-release-state`, in release: only the release executor App may create, update or delete
  `refs/heads/release-state` (rules creation, update, deletion, non_fast_forward; the App ID as an
  `always` bypass). `qq-state-branches` still applies on top, since rulesets stack, so even the App
  cannot delete or force-push it. The plan refuses this ruleset while `[release_refs]
  bypass_integration_ids` is empty or release is not in `executor_repos`, because nobody could
  write release-state then.
  - Read-only check after the apply: `gh api repos/quirq-ai/release/rules/branches/release-state`
    lists all four rules, and `qq-release-state` (Settings > Rules) has the App as its bypass.
  - Break-glass: an org owner sets `qq-release-state` to Disabled in Settings > Rules **and**
    deletes the `QQ_RELEASE_CLIENT_ID` variable
    (`gh api -X DELETE repos/quirq-ai/release/actions/variables/QQ_RELEASE_CLIENT_ID`), which is the
    mode before the variable was first set (canary-app command 2), when workflows push release-state
    with their own token. Deleting only the variable leaves release-state writable by nobody;
    disabling only the ruleset leaves it open to any pusher.
  - To restore: first set the variable again (command 2), then re-run this apply. It reports
    `qq-release-state` as `differs`; answering "replace them" before the variable is back would
    leave release-state writable by nobody. Any later apply shows the same `differs` while the
    ruleset is disabled, so answer no to it until the variable is back.
- `qq-release-tags`, in qq: no tag (`**/*`, nested ones too) may be created, moved or deleted
  by anyone, admins and the release executor included, until qq is added to
  `[release_refs] executor_repos` (with the App installed on qq). qq's pins
  trust its tags: a version-only pin, such as xo-space's and innernet's `[qq] version = "0.1.0"`,
  installs tag `v<version>`, and a `git:` commit must be on main or a `v*` tag (qq #16; main
  is locked by `qq-main`). So qq `v0.1.0` cannot be cut, by hand or otherwise, until then
  (neither product's CI installs qq yet). Other qq repos pin each other by
  commit and toolchains checks digests, so no other pins trust tags. xo-space's `v*` tags start its container publish; they are not locked, because
  suraj cuts them by hand (TODO(suraj): who may create them).
- Not in this apply: `qq-dependabot-branches` (only Dependabot may push to or force-push
  `dependabot/**/*` in xo-space and innernet). It is built only for repos with
  `dependabot_branches = true`, which is false for both today. Its bypass names the Dependabot app
  by id 29110, which could not be checked from here (`gh api /apps/dependabot --jq .id` shows the
  real one), and it is what lets rollers auto-land Dependabot rolls, which stay off until rollers'
  ROL-R6 clears. Turning it on, with the id checked, is a reviewed gate PR. No ruleset on `~ALL` or
  `refs/heads/**` may carry both `update` and `non_fast_forward`, which would satisfy rollers'
  land check the same way.

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

Status (from `settings verify` on fresh clones, 2026-10-07 15:37 UTC, gate `2a73334`): all 16
repos ready, website included. `verify` prints each repo's commit, so a repo that moves between the
clone and the run shows as not ready; the script clones it again.

Last run: suraj ran the command at `6610664`, which applied the repo rulesets. Settings merged since
(#25 toolchains' `promotion-gate`, #26 the release executor bypass, #28 website) take effect only at
the next run, and none is recorded yet. #24 `allow_auto_merge` was set by hand on 2026-10-04: it is
on in every repo but xo-space and website, and the next run sets website. Until then website has no
`qq-main` ruleset and no repo has the release executor bypass.

## Org rulesets (`--org`, needs admin:org)

**All off.** quirq-ai is on GitHub Free, which has no org rulesets (the first run's org step got
"Upgrade to GitHub Team to enable this feature"), and suraj chose no org-wide rules (2026-10-04).
GitHub's docs also list "Require workflows to pass before merging" for Enterprise Cloud only. What
they would add, and what guards those paths without them, is under "Without org rulesets" below.
The rest of this section describes them for a paid plan.

`[[org_workflows]]` in `settings/github.toml` lists org rulesets that run a workflow from another
repo on every PR and queue entry, so a PR cannot satisfy them with its own same-named job. Turning
one on or off is a reviewed change to that file, after which the same command applies it.

- `qq-xo-space-presubmit-pinned`, `qq-innernet-presubmit-pinned` and `qq-website-presubmit-pinned` (off): each product repo's presubmit,
  run from infra-config's `.github/workflows/qq-required-<repo>-presubmit.yml` at a pinned commit
  (`sha`), so neither a PR nor a dependency roll can change the workflow that judges it. rollers
  auto-lands only into a repo that has one. xo-space and innernet are pinned at infra-config `eaa2c88` (#19), whose files
  dropped `cancel-in-progress`, and website at `41a8cb0` (#32; #31 added its file); all three pass the checks below on a fresh clone. The pin fixes the workflow file, not
  the code it runs: owner review of tests and scripts is V0-GAT-03 (waits on ORG-02 owners).
- `qq-toolchains-promotion-gate` (off): toolchains' `promotion-gate.yml`, pinned at `eb71c8e`
  (toolchains #13: no `cancel-in-progress`, a 35-minute timeout). The pin fixes the workflow file;
  the gate tools it runs (`tools/gate.py`) still come from toolchains `main`, where `/tools/` needs
  suraj's code-owner approval (toolchains #14).
- `qq-drift` (off): infra-config's `qq-drift.yml` in the product repos. The fixes it waited on are
  merged in infra-config (#8: default branch only; #24: leaves rollers' `qq-roll-land.yml` alone;
  #25, #26: checks a pinned config commit). It is off for the same reason as the others; turning it
  on means pinning it and enabling it in a reviewed gate PR.

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
App tokens); PRs already open get it on their next push. The org's plan does not offer them (above).

### Without org rulesets

- A repo's required checks match a job name from GitHub Actions, so a PR that edits its own
  workflows could make a same-named job pass. What stops that is review of `.github/`: toolchains
  requires suraj's code-owner review there (toolchains #14); xo-space, innernet and website do not
  yet, and release does not by suraj's choice (fully autonomous, 2026-10-05), though only workflows
  on its main can use the release executor's key.
- toolchains' promotion gate is a repo required check (`promotion-gate`, on `pull_request` and
  `merge_group` since toolchains #15). A PR's own workflows can fake it only by editing `.github/`,
  which needs suraj's code-owner review; a push after his approval dismisses it
  (`dismiss_stale_reviews_on_push`). Its tools come from main (or the queue base), so a `tools/`
  edit cannot pass its own PR. Any GitHub Actions run with `checks: write`, such as one from a pushed
  branch, could still post a passing `promotion-gate` (or `ci`); only a required workflow (an org
  ruleset, paid plan) closes that.
- rollers lands a Dependabot roll on its own only into a repo with a gate no workflow can fake (a
  pinned required workflow, or a check from an App other than GitHub Actions), so it stays off.

**If a required workflow blocks every PR** (it fails or never reports): an org owner opens
github.com/organizations/quirq-ai/settings/rules, opens that ruleset and sets Enforcement to
Disabled (or Evaluate, which reports without blocking). Then fix the file, re-pin, and run the
command again; it sets the ruleset back to active.

## Repo settings

`allow_auto_merge` on each `[[repo]]` is applied like a ruleset: the dry run shows
`plan     quirq-ai/<repo>: update setting allow_auto_merge = true (now false)` when GitHub differs,
the same `yes` writes it (a PATCH of the repo), and a re-run shows it `unchanged`. With the merge
queue on, auto-merge is how agent sessions put a PR in the queue (they have no GraphQL); it skips no
required check or review. It is on in every repo but xo-space, where suraj lands PRs with his own
"Merge when ready" (2026-10-04), and website, which the next run sets.

## Decisions for suraj (`TODO(suraj)` in `settings/github.toml`)

- Squash merges for every repo: confirmed 2026-10-04.
- Whether admins get a break-glass bypass on `main`. The default is none. An org owner can still
  disable or edit a ruleset in Settings > Rules (the next run reports that as `differs`).
- The release executor's App ID (quirq-release-executor) for `lkgr`, `channels/**/*` and
  `release-state`, in `[release_refs] bypass_integration_ids`: set to 5199903 on 2026-10-05, from
  command 1's output. The client id (`Iv23...`) is refused, but an installation id is a number too
  and would be accepted silently, so a later change must copy the App ID from the App's General page.
- Approvals: per repo. `[main] required_approvals = 0` is the default, and the 13 infra repos keep
  it: their PRs land on green checks alone. Product repos (xo-space, innernet, website) set
  `required_approvals = 1` (suraj, alpha decision 3, 2026-10-06). GitHub never counts the PR
  author's own approval, and a push after an approval dismisses it. So an agent PR, opened by the
  agent account, needs suraj's approval after the agent's last push. A product PR opened under
  suraj's own account needs another account with write access, which on innernet and website may
  be only the agent account. Code-owner review on tests and `infra/` is V0-GAT-03.

Done-when check after applying: `git push origin HEAD:main` to any written repo is refused, and a PR
lands only through the merge queue.

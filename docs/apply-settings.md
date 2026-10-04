# Applying the gate's settings (V0-ORG-03, admin: suraj)

The merge queue and rulesets are code in `settings/github.toml`; `qqgate settings` turns them into
GitHub rulesets. An agent cannot apply them (it would be changing its own gate); an org admin runs
the steps below, from the gate commit the coordinator names.

**Token.** The repo rulesets need a token that can administer each repo: `gh auth token` from an
org owner is enough. They do **not** need `admin:org`, and you do **not** pass `--org` for them. Do
not run `gh auth refresh -s admin:org` for this step.

```sh
COMMIT=<the gate commit you were given>
rm -rf gate && git clone https://github.com/quirq-ai/gate && cd gate && git checkout --quiet "$COMMIT"

# infra-config at the commit gate pins (apply refuses any other commit)
git clone --quiet https://github.com/quirq-ai/infra-config .qq/infra-config
git -C .qq/infra-config checkout --quiet "$(python3 -c "import tomllib;print(tomllib.load(open('pins.toml','rb'))['infra-config']['commit'])")"

# hashed, fully pinned dependencies only; nothing is resolved at install time
python3 -m venv .venv
.venv/bin/pip install --require-hashes --only-binary :all: -r apply-requirements.txt
.venv/bin/pip install --no-deps --no-build-isolation -e .

# a fresh clone of every repo's default branch, every time (verify refuses stale checkouts)
rm -rf .qq/repos && mkdir -p .qq/repos
for r in $(python3 -c "import tomllib;print(' '.join(r['name'] for r in tomllib.load(open('settings/github.toml','rb'))['repo']))"); do
  git clone --quiet --depth 1 "https://github.com/quirq-ai/$r" ".qq/repos/$r"; done

.venv/bin/qqgate settings verify --config .qq/infra-config --checkouts .qq/repos     # which repos are ready
QQ_GITHUB_TOKEN=$(gh auth token) .venv/bin/qqgate settings apply --config .qq/infra-config --checkouts .qq/repos         # dry run
QQ_GITHUB_TOKEN=$(gh auth token) .venv/bin/qqgate settings apply --config .qq/infra-config --checkouts .qq/repos --yes   # write
```

What the run does and does not do:

- It only writes repos that are ready, and it skips the rest with the reason. It exits 1 while any
  repo is not ready, even after writing the ready ones, so re-run it as more repos become ready
  (re-clone `.qq/repos` first, as above).
- Every request goes to `https://api.github.com`. A redirect is refused rather than followed, so
  the token cannot reach another host (a renamed repo, which GitHub answers with a redirect, stops
  the run with an error instead).
- The plan is computed in a child process whose environment holds only `PATH`, locale and temp
  settings, with an empty `HOME`: no token, SSH agent, netrc, `gh` or git config. Only that child
  runs infra-config's code. It still runs as your OS user, so what makes its code trustworthy is the
  pinned infra-config commit (apply refuses any other commit, or local changes). The process holding
  your token reads `settings/github.toml` before the child starts (and refuses if it changes), takes
  only product repos' required check names and two gate.toml numbers from the child, checks that each
  name is a job in a workflow the pinned infra-config generates and that the transitional checks come
  last, and builds every ruleset itself.
- The dry run sends only GETs. It prints a WARNING for classic branch protection or other rulesets
  already on a repo (they stack with ours: remove them, or check their required checks run on
  `merge_group`) and for a repo with squash merging turned off (the merge queue squashes).
- Each write is printed as soon as it succeeds. If one fails, the run stops with `FAILED`, says
  every line above it is already live and lists the repos it did not attempt. Re-running is safe:
  rulesets are created or updated by name, never deleted.
- It never touches rulesets or protection it did not create, and never deletes one: a ruleset
  dropped from `settings/github.toml` (for example a repo's `state_branches` emptied) stays live
  until an admin deletes it in Settings > Rules.

Each ready repo gets:

- `qq-main` on the default branch: pull request required, merge queue (squash, all-green grouping,
  verdict timeout = gate.toml's 40-minute admission limit), the required checks (each pinned to the
  GitHub Actions app), no force push, no deletion, and nobody on the bypass list. Together these
  refuse a direct push to `main`. toolchains also requires code-owner review, which takes effect for
  the paths its CODEOWNERS gives owners (none are named yet: ORG-02).
- `qq-release-refs-branches` and `qq-release-refs-tags`: `lkgr` and `channels/**` cannot be created,
  moved or deleted except by the release executor. Its identity is not decided yet, so today nobody
  can write them.
- `qq-state-branches`, only where `state_branches` names some (gardener `ledger` and `tree-status`,
  release `release-state`): those branches cannot be deleted or force-pushed. Their bots still push
  to them normally.

`verify` (and so `apply`) refuses a repo when:

- its checkout is not a fresh clone of the default branch's current head, or the repo has no
  commits (qq-main would refuse the push that creates `main`: push a first commit first);
- it has no required check (its queue would land anything);
- its checkout was cloned from anywhere but `https://github.com/quirq-ai/<repo>`;
- a required check can skip, pass without checking, or never report: a job-level `if:` other than
  exactly `always()` (unless listed in `allow_conditional` with a reason), `needs:` without exactly
  `always()`, an `always()` job after `needs:` that never reads `needs.<job>.result`, a matrix job, a
  job that calls a reusable workflow, a path-filtered workflow, a `pull_request` branch (including
  `!` patterns) or type filter that leaves out PRs into the default branch, `merge_group` types
  without `checks_requested`, or a name used by two jobs.

What blocks which repo today (from `settings verify` on fresh clones, 2026-10-04 13:55 UTC):

- Every repo: ready. `verify` prints each repo's commit, so a repo that moves between your
  clone and the run shows as not ready; clone again and re-run.

## Org rulesets (optional, separate, needs `admin:org`)

`settings/github.toml` `[[org_workflows]]` lists org rulesets that run a workflow from another
repo's `main` on every PR and queue entry, so a PR cannot satisfy them with its own same-named job:

- `qq-toolchains-promotion-gate` (enabled): toolchains' `promotion-gate.yml` in toolchains.
- `qq-xo-space-presubmit-pinned` and `qq-innernet-presubmit-pinned` (off): each product repo's
  presubmit, run from infra-config's `.github/workflows/qq-required-<repo>-presubmit.yml` at a pinned
  commit (`sha`), so neither a PR nor a dependency roll can change the workflow that judges it.
  rollers auto-lands only into a repo that has one. They are switched on, with their `sha`, once
  infra-config publishes those files. `apply --org` checks that the pinned commit is on
  infra-config's `main` and that the file there runs on `pull_request` and `merge_group` with no path
  filter and no job that can skip or pass on failure (the only `if:` allowed is the repository guard).
- `qq-drift` (off): infra-config's `qq-drift.yml` in the product repos. It stays off until
  infra-config's `qq-drift.yml` only checks the default branch against a pinned config; turning it on
  is a reviewed one-line change.

A ruleset workflow runs only if the repo holding it lets the org's repos use its workflows
(infra-config: Settings > Actions > General > Access). If it cannot run, every PR into the targets
waits on it, so check that setting before enabling one.

Applying them is a second run, after the repo rulesets: `gh auth refresh -s admin:org`, then the
same `apply` command with `--org` added (dry run first). An org ruleset only targets repos that are
ready in that run. TODO(suraj): confirm the org's plan offers "Require workflows to pass before
merging" in rulesets.

## Decisions for suraj (`TODO(suraj)` in `settings/github.toml`)

- Squash merges for every repo (gate.toml).
- Whether admins get a break-glass bypass on `main`. The default is none. An org owner can still
  disable or edit a ruleset in Settings > Rules.
- The release executor's identity (a GitHub App id) for `lkgr` and `channels/**`.
- Approvals: `required_approvals = 0` today, because every PR here comes from an agent account and
  you cannot approve your own PRs. Code-owner review on tests and `infra/` is V0-GAT-03.

Done-when check after applying: `git push origin HEAD:main` to any written repo is refused, and a PR
lands only through the merge queue.

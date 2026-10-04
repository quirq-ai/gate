# Applying the gate's settings (V0-ORG-03, admin: suraj)

The merge queue and rulesets are code in `settings/github.toml`; `qqgate settings` turns them into
GitHub rulesets. An agent cannot apply them (it would be changing its own gate); an org admin runs:

```sh
git clone https://github.com/quirq-ai/gate && cd gate
git clone https://github.com/quirq-ai/infra-config .qq/infra-config
git -C .qq/infra-config checkout "$(python3 -c "import tomllib;print(tomllib.load(open('pins.toml','rb'))['infra-config']['commit'])")"
python3 -m venv .venv && .venv/bin/pip install -e .
mkdir -p .qq/repos && for r in $(python3 -c "import tomllib;print(' '.join(r['name'] for r in tomllib.load(open('settings/github.toml','rb'))['repo']))"); do
  git clone --depth 1 "https://github.com/quirq-ai/$r" ".qq/repos/$r"; done

.venv/bin/qqgate settings verify --config .qq/infra-config --checkouts .qq/repos     # which repos are ready
QQ_GITHUB_TOKEN=$(gh auth token) .venv/bin/qqgate settings apply --config .qq/infra-config --checkouts .qq/repos         # dry run
QQ_GITHUB_TOKEN=$(gh auth token) .venv/bin/qqgate settings apply --config .qq/infra-config --checkouts .qq/repos --yes   # write
```

Apply only writes repos that are ready, so it is safe to re-run as more repos get ready; it exits 1
while any repo is not ready, even after writing the ready ones. The dry run also prints a WARNING for
classic branch protection or other rulesets already on a repo: those stack with ours, so remove them
or check their required checks run on `merge_group`. The org ruleset is not part of this run: it stays
off (`enabled = false` in `settings/github.toml`) until infra-config's `qq-drift.yml` only checks the
default branch and compares against a pinned config. When it is turned on, run `gh auth refresh -s
admin:org` and add `--org`; it goes last, so if it fails (no `admin:org`, or the org's plan lacks
"Require workflows to pass before merging", TODO(suraj): confirm the plan) the repo rulesets are
already written. Each repo gets:

- `qq-main` on the default branch: pull request required, merge queue (squash, all-green grouping,
  verdict timeout = gate.toml's 40-minute admission limit), the required checks, no force push, no
  deletion, and nobody on the bypass list. Together these refuse a direct push to `main`.
- `qq-release-refs-branches` and `qq-release-refs-tags`: `lkgr` and `channels/**` cannot be created,
  moved or deleted except by the release executor. Its identity is not decided yet, so today nobody
  can write them.
- Later, with `--org`: the org ruleset `qq-drift`, running infra-config's `qq-drift.yml` from `main` in every
  product repo (xo-space, innernet).

What blocks which repo (from `settings verify`, 2026-10-04):

- xo-space: merge the generated workflows (xo-space #211), and add `merge_group` to `tests.yml`, whose
  `tests` check stays required until V0-ONB-01 retires it.
- innernet: merge the generated workflows (innernet #37).
- depot: `parity-compare` needs the `parity` matrix without `if: always()`, so a failed leg would skip
  it and GitHub would count the skip as a pass. The depot thread is fixing it (aggregator pattern).
- Other infra repos: ready. gardener, rollers, release and installer have no presubmit yet; their PRs
  still go through the queue, and their check is added to `settings/github.toml` when it lands.

`verify` refuses a required check that can skip or never report: a job-level `if:` (unless it is
`always()`-style or listed in `allow_conditional` with a reason), `needs:` without `always()`, a
matrix job, a path-filtered workflow, or a name used by jobs in two workflows.

Decisions for suraj (`TODO(suraj)` in `settings/github.toml`):

- Squash merges for every repo (gate.toml).
- Whether admins get a break-glass bypass on `main`. The default is none.
- The release executor's identity (a GitHub App id) for `lkgr` and `channels/**`.
- Approvals: `required_approvals = 0` today, because every PR here comes from an agent account and
  you cannot approve your own PRs. Code-owner review on tests and `infra/` is V0-GAT-03.

Done-when check after applying: `git push origin HEAD:main` to any repo is refused, and a PR lands
only through the merge queue.

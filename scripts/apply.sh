#!/usr/bin/env bash
# Apply the gate's settings: the one command in docs/apply-settings.md (V0-ORG-03, an org admin runs it).
#
#   git clone -q https://github.com/quirq-ai/gate qq-gate && cd qq-gate && git checkout -q <COMMIT> && scripts/apply.sh
#
# It sets up a hashed venv, clones infra-config at the pinned commit and every repo fresh, runs
# verify and a dry run, and writes only after you type `yes`. If an org ruleset is enabled in
# settings/github.toml it then asks gh for admin:org, applies those, and drops the scope again.
# Nothing is written before a `yes`. Re-running is safe: rulesets are created or updated by name.
set -euo pipefail

say() { printf '\n== %s\n' "$*"; }
die() { printf '\napply.sh: %s\n' "$*" >&2; exit 2; }
ask() {  # ask "question": true only when the answer is exactly yes
  local a
  read -r -p "$1 Type yes to continue: " a </dev/tty || return 1
  [ "$a" = yes ]
}

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
owner=quirq-ai

# 1. Tools, and a clean gate checkout (what you apply is exactly the commit you checked out).
command -v git >/dev/null || die "git is not installed"
command -v gh >/dev/null || die "gh (GitHub CLI) is not installed: https://cli.github.com"
command -v python3 >/dev/null || die "python3 is not installed (3.11 or newer)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || die "python3 is $(python3 -V 2>&1); qqgate needs 3.11 or newer"
gh auth status -h github.com >/dev/null 2>&1 || die "gh is not logged in to github.com: run  gh auth login"
[ -z "$(git status --porcelain --untracked-files=no)" ] || die "this gate checkout has local changes; use a clean clone"
echo "gate commit: $(git rev-parse HEAD)"

# qqgate asks `gh auth token` itself after the plan child has exited; a token in this environment
# would be readable by the child that runs infra-config's code.
if [ -n "${QQ_GITHUB_TOKEN:-}" ]; then
  echo "QQ_GITHUB_TOKEN is set; ignoring it (qqgate reads the token from gh after planning)"
  unset QQ_GITHUB_TOKEN
fi

# 2. Hashed, fully pinned dependencies only; nothing is resolved at install time.
say "Setting up .venv"
python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check --require-hashes --only-binary :all: -r apply-requirements.txt
.venv/bin/pip install -q --disable-pip-version-check --no-deps --no-build-isolation -e .
py=.venv/bin/python
qqgate=.venv/bin/qqgate

# 3. infra-config at the commit gate pins (apply refuses any other), and a fresh clone of every
#    repo's default branch (verify refuses stale checkouts).
say "Cloning infra-config at the pinned commit and every repo fresh"
pin=$("$py" -c "import tomllib; print(tomllib.load(open('pins.toml', 'rb'))['infra-config']['commit'])")
rm -rf .qq && mkdir -p .qq/repos
git clone --quiet "https://github.com/$owner/infra-config" .qq/infra-config
git -C .qq/infra-config checkout --quiet "$pin"
for r in $("$py" -c "import tomllib; print(' '.join(r['name'] for r in tomllib.load(open('settings/github.toml', 'rb'))['repo']))"); do
  git clone --quiet --depth 1 "https://github.com/$owner/$r" ".qq/repos/$r"
done
args=(--config .qq/infra-config --checkouts .qq/repos)

# Exit 0 = all good, 1 = something not ready or refused (the output says what), 2 = error.
status=0
run() {
  local rc=0
  "$@" || rc=$?
  [ "$rc" -le 1 ] || die "stopped: '$*' failed (exit $rc), see above"
  [ "$rc" = 0 ] || status=1
}
finish() {
  if [ "$status" = 0 ]; then say "$1"; exit 0; fi
  say "Finished, but something above was not ready, skipped or REFUSED (exit 1). Fix it and run this again."
  exit 1
}

# 4. Verify and dry run (GETs only).
say "Verify: which repos are ready"
run "$qqgate" settings verify "${args[@]}"
say "Dry run of the repo rulesets (reads GitHub, writes nothing)"
plan=$("$qqgate" settings apply "${args[@]}" 2>&1) || [ $? -le 1 ] || { echo "$plan"; die "the dry run failed"; }
echo "$plan"

# 5. Confirm, then write. A WARNING or a live ruleset that differs each needs its own yes.
flags=()
if grep -q '^WARNING ' <<<"$plan"; then
  ask "The WARNING lines above are protection already on those repos, which stacks with ours." \
    || die "stopped before writing; nothing was written"
  flags+=(--accept-warnings)
fi
if grep -q '(differs: ' <<<"$plan"; then
  ask "Rulesets marked 'differs' were changed on GitHub (for example in the UI); writing replaces them, removing any bypass added there." \
    || die "stopped before writing; nothing was written"
  flags+=(--overwrite)
fi
if grep -Eq '^plan .*: (create|update) ruleset' <<<"$plan"; then
  ask "Write the 'plan' lines above to GitHub?" || die "stopped before writing; nothing was written"
  say "Writing the repo rulesets"
  run "$qqgate" settings apply "${args[@]}" --yes ${flags[@]+"${flags[@]}"}
else
  echo "Every ready repo's rulesets already match settings; nothing to write."
fi

# 6. Org rulesets, only when settings/github.toml enables one (they need admin:org).
enabled=$("$py" -c "import tomllib; print(sum(1 for w in tomllib.load(open('settings/github.toml', 'rb')).get('org_workflows', []) if w.get('enabled')))")
if [ "$enabled" = 0 ]; then
  finish "No org ruleset is enabled in settings/github.toml, so there is no admin:org step. Done."
fi
ask "$enabled org ruleset(s) are enabled. They need the admin:org scope, which gh will now ask you to grant (and this script removes again at the end)." \
  || die "stopped before the org step; the repo rulesets above are applied"
had_admin_org=no
if gh auth status -h github.com 2>&1 | grep -q "admin:org"; then had_admin_org=yes; fi
drop_scope() {
  if [ "$had_admin_org" = no ]; then
    say "Removing the admin:org scope from gh again"
    gh auth refresh -h github.com --remove-scopes admin:org \
      || echo "Could not remove admin:org; run:  gh auth refresh -h github.com --remove-scopes admin:org"
  fi
}
trap drop_scope EXIT
gh auth refresh -h github.com -s admin:org
say "Dry run with the org rulesets (the repo rulesets show as unchanged)"
plan=$("$qqgate" settings apply "${args[@]}" --org 2>&1) || [ $? -le 1 ] || { echo "$plan"; die "the org dry run failed"; }
echo "$plan"
if grep -q '(differs: ' <<<"$plan" && [[ " ${flags[*]-} " != *" --overwrite "* ]]; then
  ask "Rulesets marked 'differs' were changed on GitHub; writing replaces them." || die "stopped before the org write"
  flags+=(--overwrite)
fi
if grep -Eq '^plan .*: (create|update) ruleset' <<<"$plan"; then
  ask "Write the org 'plan' lines above to GitHub?" || die "stopped before the org write"
  say "Writing the org rulesets"
  run "$qqgate" settings apply "${args[@]}" --org --yes ${flags[@]+"${flags[@]}"}
else
  echo "Nothing to write for the org rulesets."
fi
finish "Done."

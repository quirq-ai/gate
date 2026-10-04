#!/usr/bin/env bash
# Apply the gate's settings: the one command in docs/apply-settings.md (V0-ORG-03, an org admin runs it).
# Run from the same directory every time; it reuses ./qq-gate when it exists:
#
#   { [ -d qq-gate ] || git clone -q https://github.com/quirq-ai/gate qq-gate; } && cd qq-gate && git fetch -q origin && git checkout -q <COMMIT> && scripts/apply.sh
#
# It sets up a hashed venv, clones infra-config at the pinned commit and every repo fresh, runs
# verify and a dry run, and writes only after you type `yes`. A repo that moves during the run is
# cloned again and offered again. If an org ruleset is enabled in settings/github.toml it then asks
# gh for admin:org, applies those, and drops the scope again; a re-run of the same commit that
# already applied them skips that step. Nothing is written before a `yes`. Re-running is safe:
# rulesets are created or updated by name.
set -euo pipefail

say() { printf '\n== %s\n' "$*"; }
die() { printf '\napply.sh: %s\n' "$*" >&2; exit 2; }
stop() { printf '\napply.sh: %s\n' "$*" >&2; exit 1; }  # you answered something other than yes
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
if [ -n "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ]; then
  die "GH_TOKEN or GITHUB_TOKEN is set; unset it and use gh's own login (gh auth login), which the org step refreshes"
fi
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
clone() { rm -rf ".qq/repos/$1" && git clone --quiet --depth 1 "https://github.com/$owner/$1" ".qq/repos/$1"; }
for r in $("$py" -c "import tomllib; print(' '.join(r['name'] for r in tomllib.load(open('settings/github.toml', 'rb'))['repo']))"); do
  clone "$r"
done
args=(--config .qq/infra-config --checkouts .qq/repos)
log=.qq/out.txt

# Exit 0 = all good, 1 = something not ready or refused (the output says what), 2 = error.
status=0
finish() {
  if [ "$status" = 0 ]; then say "$1"; exit 0; fi
  say "Finished, but something above was not ready, skipped or REFUSED (exit 1). Fix it and run this again."
  exit 1
}
# Repos whose checkout fell behind their default branch during the run ("clone again"), and
# whether anything else in the output needs you (another NOT READY reason, a skip, a refusal).
moved() { awk '/^NOT READY /{r=$3} /clone again/{print r}' "$log" | sort -u; }
other_problem() {
  awk '/^NOT READY /{r=$3; nr[r]=1} /clone again/{st[r]=1} /^(skip|REFUSED) /{bad=1}
       END{for (k in nr) if (!(k in st)) bad=1; exit bad ? 0 : 1}' "$log"
}
qq() {  # qq <args...>: run qqgate, showing and keeping its output; 2 (an error) stops the script
  local rc=0
  "$qqgate" "$@" 2>&1 | tee "$log" || rc=$?
  [ "$rc" -le 1 ] || die "stopped: 'qqgate $*' failed (exit $rc), see above"
}

# 4. Verify, cloning again any repo that moved since its clone (up to three times).
say "Verify: which repos are ready"
for _ in 1 2 3; do
  qq settings verify "${args[@]}"
  again=$(moved)
  [ -n "$again" ] || break
  say "Cloning again what moved: $(echo $again)"
  for r in $again; do clone "$r"; done
done

# phase <what> [qqgate args...]: dry run (GETs only), a yes for any WARNING, a yes for any ruleset
# that differs, a yes for the plan, then the write. A repo that moved between clone and write is
# cloned again and offered again on its own, up to three rounds.
phase() {
  local what=$1 round only=() f again bad=0 org=no
  case " $* " in *" --org "*) org=yes ;; esac
  shift
  for round in 1 2 3; do
    say "Dry run of $what (reads GitHub, writes nothing)"
    qq settings apply "${args[@]}" "$@" ${only[@]+"${only[@]}"}
    cp "$log" .qq/dry.txt
    f=()
    if grep -q '^WARNING ' .qq/dry.txt; then
      ask "The WARNING lines above are protection already on those repos, which stacks with ours." \
        || stop "stopped before writing $what; nothing more was written"
      f+=(--accept-warnings)
    fi
    if grep -q '(differs: ' .qq/dry.txt; then
      ask "Rulesets marked 'differs' are not what settings/github.toml says (settings changed, or someone edited them on GitHub); writing replaces them, removing any bypass added there." \
        || stop "stopped before writing $what; nothing more was written"
      f+=(--overwrite)
    fi
    if grep -Eq '^plan .*: (create|update) ruleset' .qq/dry.txt; then
      ask "Write every 'plan' line above to GitHub (for $what, and any repo ruleset listed with them)?" || stop "stopped before writing $what; nothing more was written"
      say "Writing $what"
      qq settings apply "${args[@]}" "$@" ${only[@]+"${only[@]}"} --yes ${f[@]+"${f[@]}"}
    else
      echo "Nothing to write for $what."
    fi
    cat .qq/dry.txt "$log" > .qq/both.txt && mv .qq/both.txt "$log"
    # A repo round re-plans only what moved, so what earlier rounds found still counts. The org
    # round re-plans everything, so only its last round counts (an org ruleset skipped because its
    # repo moved is written in the next round).
    if [ "$org" = yes ]; then bad=0; fi
    other_problem && bad=1
    again=$(moved)
    if [ -z "$again" ]; then
      [ "$bad" = 0 ] || status=1
      return 0
    fi
    say "These moved during the run and are cloned again: $(echo $again)"
    only=()
    for r in $again; do
      clone "$r"
      # The org run always plans every repo: limited to some, it would skip the org rulesets of the rest.
      [ "$org" = yes ] || only+=(--repo "$r")
    done
  done
  echo "Still moving after three rounds: $(echo $again). Run the command again later."
  status=1
}

# 5. Repo rulesets.
phase "the repo rulesets"

# 6. Org rulesets, only when settings/github.toml enables one (they need admin:org). A re-run of
#    a commit whose org rulesets an earlier run applied skips this step and its browser prompts.
enabled=$("$py" -c "import tomllib; print(sum(1 for w in tomllib.load(open('settings/github.toml', 'rb')).get('org_workflows', []) if w.get('enabled')))")
if [ "$enabled" = 0 ]; then
  finish "No org ruleset is enabled in settings/github.toml, so there is no admin:org step. Done."
fi
done_mark=.apply-org-done   # untracked; holds the gate commit whose org rulesets were applied
if [ "$(cat "$done_mark" 2>/dev/null || true)" = "$(git rev-parse HEAD)" ]; then
  finish "The org rulesets of this commit were applied by an earlier run, so the admin:org step is skipped (it did not check them for edits made on GitHub since; delete $done_mark to run it again). Done."
fi
ask "$enabled org ruleset(s) are enabled. They need the admin:org scope, which gh will now ask you to grant (and this script removes again at the end)." \
  || stop "stopped before the org step; the repo rulesets above are applied"
gh_status=$(gh auth status -h github.com 2>&1 || true)
had_admin_org=no
case "$gh_status" in *admin:org*) had_admin_org=yes ;; esac
drop_scope() {
  if [ "$had_admin_org" = no ]; then
    say "Removing the admin:org scope from gh again"
    gh auth refresh -h github.com --remove-scopes admin:org \
      || echo "Could not remove admin:org; run:  gh auth refresh -h github.com --remove-scopes admin:org"
  fi
}
if [ "$had_admin_org" = no ]; then
  trap drop_scope EXIT
  gh auth refresh -h github.com -s admin:org
fi
# The org run plans the repo rulesets again (they show as unchanged) and asks again: a yes for the
# repo rulesets is not a yes for the org ones.
before=$status
status=0
phase "the org rulesets" --org
[ "$status" = 0 ] && git rev-parse HEAD > "$done_mark"
[ "$before" = 0 ] || status=1
finish "Done."

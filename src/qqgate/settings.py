"""V0-ORG-03: merge queue and rulesets as code (settings/<backend>.toml), applied by an admin.

`build` turns settings plus infra-config into one desired ruleset list per repo; product repos get
their required checks from V0-GAT-01 (`required.compute`). `readiness` says whether applying a repo's
rulesets is safe today: every required check must be a job in that repo's default branch that runs
on both pull_request and merge_group, or the queue would wait forever on a check that never reports.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from qqgate import backends, required
from qqgate.errors import GateError

SETTINGS = Path(__file__).resolve().parents[2] / "settings"
GATE_EVENTS = ("pull_request", "merge_group")


@dataclass(frozen=True)
class RepoPlan:
    name: str
    kind: str                 # product | infra
    checks: tuple[str, ...]   # every required check, gate-computed first
    rulesets: tuple[dict, ...]


def load_settings(backend: str, path: Path | None = None, data: dict | None = None) -> dict:
    """Settings from `path` (default: gate's own settings/<backend>.toml), checked; `data` is that
    file already parsed, so a caller that checked its bytes checks the same content."""
    path = path or SETTINGS / f"{backend}.toml"
    if data is None:
        if not path.is_file():
            raise GateError(f"no settings for backend {backend!r} ({path})")
        with path.open("rb") as f:
            data = tomllib.load(f)
    patterns = {"release_refs.branches": data.get("release_refs", {}).get("branches", []),
                "release_refs.tags": data.get("release_refs", {}).get("tags", []),
                "reserved_tags.names": data.get("reserved_tags", {}).get("names", []),
                "dependabot.branches": [data["dependabot"]["branches"]] if "dependabot" in data else []}
    for where, values in patterns.items():
        bad = [p for p in values if not _matches_nested(p)]
        if bad:
            raise GateError(f"{where}: {bad!r} {_NESTED}")
    if path == SETTINGS / f"{backend}.toml" and "release_refs" not in data:
        # Optional only in a user org's file (`--settings`), which gets no release-refs rulesets.
        raise GateError(f"{path}: [release_refs] is missing")
    if "release_refs" in data:
        refs = data["release_refs"]
        taken = {"qq-state-branches", "qq-release-tags", f"{refs.get('ruleset')}-branches",
                 f"{refs.get('ruleset')}-tags"} | {data.get(k, {}).get("ruleset") for k in (
                     "main", "reserved_tags", "dependabot")} | {w.get("ruleset") for w in data.get("org_workflows", [])}
        _check_release_refs(refs, [r.get("name") for r in data.get("repo", [])], taken)
    return data


# A user org's settings (`qqgate settings --settings FILE`, the one-command setup in quirq-ai/setup)
# get only qq-main and qq-reserved-tags, under these fixed names: gate treats rulesets of those names as
# its own, and setup's undo text names them. Anything else (release refs, Dependabot, org rulesets,
# per-repo options) is quirq's own settings/github.toml only.
USER_RULESETS = {"main": "qq-main", "reserved_tags": "qq-reserved-tags"}
USER_CONFIG_REPO = "qq-config"   # a user org's one infra repo (infra-config qqcfg USER_CONFIG_REPO)
_USER_KEYS = {"org": {"owner"}, "main": {"ruleset", "merge_method", "required_approvals", "bypass"},
              "reserved_tags": {"ruleset", "names"}}
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")
_CHECK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,99}")


def _name_ok(name: object) -> bool:
    """A repo or tag name GitHub accepts: not `.` or `..`, and not ending in `.git`."""
    return (isinstance(name, str) and bool(_REPO.fullmatch(name)) and name not in (".", "..")
            and not name.lower().endswith(".git"))


def load_user_settings(path: Path, raw: bytes | None = None) -> dict:
    """A user org's settings file, checked so that it can only ever write qq-main and
    qq-reserved-tags to its own org's repos. quirq-ai's rulesets come only from the reviewed
    settings/github.toml (CODEOWNERS), so its owner is refused here in any letter case. The file is
    read and parsed once (`raw`: bytes the caller already read), so every check sees one content."""
    try:
        raw = Path(path).read_bytes() if raw is None else raw
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise GateError(f"--settings {path}: {e}") from None
    extra = sorted(set(data) - {*_USER_KEYS, "repo"})
    if extra:  # before load_settings, which would check a [release_refs] it then accepts
        raise GateError(f"{path}: sections {extra} are not allowed in a user org's settings; only [org], [main], "
                        "[reserved_tags] and [[repo]]")
    data = load_settings("github", Path(path), data)
    builtin = load_settings("github")["org"]["owner"]
    for section, keys in _USER_KEYS.items():
        if not isinstance(data.get(section), dict):
            raise GateError(f"{path}: [{section}] is missing")
        bad = sorted(set(data[section]) - keys)
        if bad:
            raise GateError(f"{path}: [{section}] keys {bad} are not allowed; only {sorted(keys)}")
    owner = data["org"].get("owner")
    if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
        raise GateError(f"{path}: [org] owner must be a GitHub org name, not {owner!r}")
    if owner.lower() == builtin.lower():
        raise GateError(f"{path}: [org] owner {owner!r} is {builtin}, whose rulesets come only from gate's own "
                        "settings/github.toml")
    if owner != owner.lower():  # as qqcfg's code_host, and the clone URLs readiness compares exactly
        raise GateError(f"{path}: [org] owner {owner!r} must be lower case ({owner.lower()!r})")
    for section, name in USER_RULESETS.items():
        if data[section].setdefault("ruleset", name) != name:
            raise GateError(f"{path}: [{section}] ruleset must be {name!r}, not {data[section]['ruleset']!r}")
    approvals = data["main"].get("required_approvals")
    if isinstance(approvals, bool) or not isinstance(approvals, int) or not 0 <= approvals <= 10:
        raise GateError(f"{path}: [main] required_approvals must be an integer from 0 to 10, not {approvals!r}")
    if not isinstance(data["main"].get("merge_method"), str):
        raise GateError(f"{path}: [main] merge_method is missing (gate.toml's, squash)")
    if data["main"].setdefault("bypass", []) != []:
        raise GateError(f"{path}: [main] bypass must stay empty: nobody overrides the gate")
    names = data["reserved_tags"].get("names")
    if not isinstance(names, list) or not names or not all(_name_ok(n) for n in names):
        raise GateError(f"{path}: [reserved_tags] names must be a non-empty list of tag names, not {names!r}")
    repos = data.get("repo")
    if not isinstance(repos, list) or not repos:
        raise GateError(f"{path}: no [[repo]] entries")
    for r in repos:
        name, kind = r.get("name"), r.get("kind")
        if not _name_ok(name):
            raise GateError(f"{path}: [[repo]] name must be a repo name, not {name!r}")
        keys = {"name", "kind", "checks"} if name == USER_CONFIG_REPO else {"name", "kind"}
        if kind != ("infra" if name == USER_CONFIG_REPO else "product"):
            raise GateError(f"{path}: {name}: kind must be 'product' (or 'infra' for {USER_CONFIG_REPO!r} only), "
                            f"not {kind!r}")
        bad = sorted(set(r) - keys)
        if bad:
            raise GateError(f"{path}: {name}: keys {bad} are not allowed; only {sorted(keys)}")
        checks = r.get("checks", [])
        if not isinstance(checks, list) or not all(isinstance(c, str) and _CHECK.fullmatch(c) for c in checks):
            raise GateError(f"{path}: {name}: checks must be a list of check names, not {checks!r}")
    if [r["name"] for r in repos if r["kind"] == "infra"] != [USER_CONFIG_REPO]:
        raise GateError(f"{path}: list exactly one infra repo, {USER_CONFIG_REPO!r}")
    return data


def check_user_owner(s: dict, code_host: object) -> None:
    """A user org's settings write only to the org its qq-config names (org.toml code_host).
    GitHub owner names ignore case."""
    owner = s["org"]["owner"]
    host = code_host if isinstance(code_host, str) else ""
    if not host.startswith("github.com/") or host.removeprefix("github.com/").lower() != owner.lower():
        raise GateError(f"settings [org] owner {owner!r} is not the owner in the config's code_host {code_host!r}")


def _check_release_refs(refs: dict, repos: list, taken: set) -> None:
    """The release executor's bypass: GitHub App ids (positive integers, the App ID, not its client id
    or an installation id), and the repos it is installed on, the only ones that get the bypass."""
    ids = refs.get("bypass_integration_ids")
    if not isinstance(ids, list) or len(set(map(repr, ids))) != len(ids) or not all(
            isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in ids):
        raise GateError(f"release_refs.bypass_integration_ids must be distinct positive integers (GitHub "
                        f"App IDs), not {ids!r}")
    where = refs.get("executor_repos", [])
    if not isinstance(where, list) or len(set(map(repr, where))) != len(where) or not all(
            isinstance(n, str) for n in where):
        raise GateError(f"release_refs.executor_repos must be distinct repo names, not {where!r}")
    name = refs.get("state_ruleset", "qq-release-state")
    if not isinstance(name, str) or not re.fullmatch(r"qq-[a-z0-9-]+", name) or name in taken:
        raise GateError(f"release_refs.state_ruleset must be its own qq-* ruleset name, not {name!r}")
    unknown = sorted(set(where) - set(repos))
    if unknown:
        raise GateError(f"release_refs.executor_repos names repos not listed in settings: {unknown}")


# GitHub matches ruleset ref patterns with fnmatch and FNM_PATHNAME (its ruleset docs), where a `**` that
# is not followed by `/` matches one path level only: `refs/tags/**` misses `refs/tags/x/y` (gate #15
# review). `**/*` matches every level.
_NESTED = "uses a `**` not followed by `/`, which matches one level only; write `**/*` for every level"


def _matches_nested(pattern: str) -> bool:
    return not re.search(r"\*\*(?!/)", pattern)


def _repos_in_config(cfg: dict) -> tuple[set[str], set[str]]:
    infra = {r["name"] for r in cfg["org"]["infra_repo"]}
    product = {r["name"] for r in cfg["repos"]["repo"]}
    return infra, product


def build(settings: dict, cfg: dict, config_root: Path) -> list[RepoPlan]:
    backend = cfg["gate"]["merge_queue"]["backend"]
    mod = backends.load(backend)
    infra, product = _repos_in_config(cfg)
    listed = [r["name"] for r in settings["repo"]]
    dupes = sorted({n for n in listed if listed.count(n) > 1})
    if dupes:
        raise GateError(f"settings list these repos twice: {dupes}")
    for name, want in (("infra", infra), ("product", product)):
        got = {r["name"] for r in settings["repo"] if r["kind"] == name}
        if got != want:
            raise GateError(f"settings {name} repos {sorted(got)} differ from infra-config's {sorted(want)}: "
                            f"missing {sorted(want - got)}, unknown {sorted(got - want)}")
    plans = []
    for r in settings["repo"]:
        if r["kind"] == "product":
            req = required.compute(cfg, r["name"])
            problems = mod.check_workflows(config_root, req)
            if problems:
                raise GateError(f"{r['name']}: " + "; ".join(problems))
            checks = tuple(req.names) + tuple(r.get("transitional_checks", []))
        else:
            checks = tuple(r.get("checks", []))
        if len(set(checks)) != len(checks):
            raise GateError(f"{r['name']}: a required check is listed twice: {checks}")
        repo_settings(r)  # type-checked here too, so verify refuses a bad value
        rulesets = mod.rulesets(settings, cfg, checks, **repo_options(r, settings))
        plans.append(RepoPlan(r["name"], r["kind"], checks, tuple(rulesets)))
    return plans


def repo_settings(r: dict) -> dict:
    """Repo settings (not rulesets) from settings/<backend>.toml that apply writes, type-checked.
    allow_auto_merge: with the merge queue on, auto-merge is how agent sessions queue a PR."""
    out = {}
    if "allow_auto_merge" in r:
        if not isinstance(r["allow_auto_merge"], bool):
            raise GateError(f"{r['name']}: allow_auto_merge must be true or false, not {r['allow_auto_merge']!r}")
        out["allow_auto_merge"] = r["allow_auto_merge"]
    return out


def repo_options(r: dict, settings: dict | None = None) -> dict:
    """Per-repo ruleset options from settings/<backend>.toml, type-checked. With `settings`, also
    whether the release executor is installed on this repo ([release_refs] executor_repos)."""
    owners, size = r.get("code_owner_review", False), r.get("queue_group_size", 5)
    if not isinstance(owners, bool):
        raise GateError(f"{r['name']}: code_owner_review must be true or false, not {owners!r}")
    if isinstance(size, bool) or not isinstance(size, int):
        raise GateError(f"{r['name']}: queue_group_size must be an integer, not {size!r}")
    # Per repo; a repo without it takes [main] required_approvals. GitHub allows 0 to 10.
    approvals = r.get("required_approvals")
    if approvals is not None and (isinstance(approvals, bool) or not isinstance(approvals, int)
                                  or not 0 <= approvals <= 10):
        raise GateError(f"{r['name']}: required_approvals must be an integer from 0 to 10, not {approvals!r}")
    state = r.get("state_branches", [])
    if not isinstance(state, list) or len(set(state)) != len(state) or not all(
            isinstance(b, str) and re.fullmatch(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*", b)
            and b not in ("main", "HEAD") and not b.startswith("refs/") for b in state):
        raise GateError(f"{r['name']}: state_branches must be distinct plain branch names (no patterns, "
                        f"refs/ or HEAD, not main), not {state!r}")
    tags = r.get("release_tags", [])
    if not isinstance(tags, list) or len(set(tags)) != len(tags) or not all(
            isinstance(t, str) and re.fullmatch(r"[A-Za-z0-9._*-]+(/[A-Za-z0-9._*-]+)*", t)
            and not t.startswith("refs/") for t in tags):
        raise GateError(f"{r['name']}: release_tags must be distinct tag name patterns (no refs/), not {tags!r}")
    if not all(_matches_nested(t) for t in tags):
        raise GateError(f"{r['name']}: release_tags {tags!r} {_NESTED}")
    mine = r.get("executor_branches", [])
    if not isinstance(mine, list) or not mine and "executor_branches" in r or not all(
            isinstance(b, str) and b in state for b in mine) or len(set(mine)) != len(mine):
        raise GateError(f"{r['name']}: executor_branches must be distinct names from its state_branches, "
                        f"not {mine!r}")
    bot = r.get("dependabot_branches", False)
    if not isinstance(bot, bool):
        raise GateError(f"{r['name']}: dependabot_branches must be true or false, not {bot!r}")
    refs = (settings or {}).get("release_refs", {})
    executor = r["name"] in refs.get("executor_repos", [])
    if mine and settings is not None and not (executor and refs.get("bypass_integration_ids")):
        # Without the App's bypass nobody could write these branches, the executor included.
        raise GateError(f"{r['name']}: executor_branches need the release executor's App ID in "
                        f"[release_refs] bypass_integration_ids and {r['name']!r} in executor_repos")
    return {"code_owner_review": owners, "required_approvals": approvals, "group_size": size,
            "state_branches": tuple(state),
            "dependabot_branches": bot, "release_tags": tuple(tags), "release_executor": executor,
            "executor_branches": tuple(mine)}


@dataclass
class Job:
    events: set[str]
    workflows: list[str]
    condition: str = ""        # job-level `if:`; a skipped job counts as passing on GitHub
    needs: tuple[str, ...] = ()
    matrix: bool = False       # matrix legs report as "name (a, b)", never as the bare name
    path_filtered: bool = False
    reusable: bool = False     # `uses:` a reusable workflow: reports as "job / inner job"
    pr_filters: list[dict] = field(default_factory=list)   # pull_request: branches / types per workflow
    mg_filters: list[dict] = field(default_factory=list)   # merge_group: types per workflow
    reads_needs: bool = False  # compares needs.<job>.result with success (an always() aggregator must)
    continue_on_error: bool = False   # job-level: a failure still reports success
    environment: bool = False  # waits on an environment's protection rules (approval, timer)


def _events(on) -> tuple[set[str], bool, dict, dict]:
    if isinstance(on, str):
        return {on}, False, {}, {}
    if isinstance(on, list):
        return set(on), False, {}, {}
    on = on if isinstance(on, dict) else {}
    filtered = any(isinstance(v, dict) and ("paths" in v or "paths-ignore" in v)
                   for k, v in on.items() if k in GATE_EVENTS)
    pr, mg = on.get("pull_request"), on.get("merge_group")
    return set(on), filtered, pr if isinstance(pr, dict) else {}, mg if isinstance(mg, dict) else {}


def workflow_jobs(checkout: Path) -> dict[str, Job]:
    """Check name (job `name:` or id) -> how it runs, across every workflow in a checkout."""
    import yaml  # here, not at the top: `queued-at` runs with nothing installed (timing action)
    jobs: dict[str, Job] = {}
    for wf in sorted((checkout / ".github" / "workflows").glob("*.y*ml")):
        try:
            doc = yaml.safe_load(wf.read_text()) or {}
        except yaml.YAMLError as e:
            raise GateError(f"{wf}: not valid YAML: {e}") from None
        if not isinstance(doc, dict):
            continue
        events, filtered, pr, mg = _events(doc.get("on", doc.get(True)))
        for job_id, job in (doc.get("jobs") or {}).items():
            job = job if isinstance(job, dict) else {}
            name = str(job.get("name", job_id))
            needs = job.get("needs", ())
            j = jobs.setdefault(name, Job(set(), []))
            j.events |= events
            j.workflows.append(wf.name)
            j.condition = str(job.get("if", "")) or j.condition
            j.needs = tuple([needs] if isinstance(needs, str) else needs) or j.needs
            j.matrix = j.matrix or "matrix" in (job.get("strategy") or {})
            j.path_filtered = j.path_filtered or filtered
            j.reusable = j.reusable or "uses" in job
            if pr:
                j.pr_filters.append(pr)
            if mg:
                j.mg_filters.append(mg)
            text = json.dumps(job, default=str)
            # A text test, not a proof: real aggregators judge results in many shapes (jq over
            # toJSON(needs.*.result), a shell test, an expression), so the job must read a result
            # and mention success, or look for failure among them. What it does with them is
            # reviewed in its own repo.
            j.reads_needs = j.reads_needs or bool(NEEDS_CHECKED.search(text) or (
                NEEDS_RESULT.search(text) and "success" in text))
            j.continue_on_error = j.continue_on_error or job.get("continue-on-error") not in (None, False)
            j.environment = j.environment or "environment" in job
    return jobs


NEEDS_RESULT = re.compile(r"needs\.(\*|[\w-]+)\.result")
# An always() aggregator that looks for failure/cancelled among its needs' results, without
# naming success.
_Q = r"""(?:\\?["'])?"""
NEEDS_CHECKED = re.compile(
    r"needs\.(?:\*|[\w-]+)\.result\s*(?:\}\})?" + _Q + r"\s*(?:==|!=|=)\s*" + _Q + r"success\b"
    r"|\bsuccess" + _Q + r"\s*(?:==|!=|=)\s*" + _Q + r"(?:\$\{\{\s*)?needs\.(?:\*|[\w-]+)\.result"
    r"|contains\(\s*needs\.\*\.result\s*,\s*" + _Q + r"(?:failure|cancelled)")


def _is_always(condition: str) -> bool:
    """Exactly `always()` (optionally in ${{ }}): anything more can still skip the job."""
    c = condition.strip()
    if c.startswith("${{") and c.endswith("}}"):
        c = c[3:-2].strip()
    return c == "always()"


def _as_list(v) -> list[str]:
    return [v] if isinstance(v, str) else [str(x) for x in (v or [])]


def _branch_selected(branch: str, patterns: list[str]) -> bool:
    """GitHub's `branches:` semantics: patterns apply in order, a `!` pattern excludes, the last
    matching pattern wins."""
    selected = False
    for p in patterns:
        if p.startswith("!"):
            if fnmatch.fnmatchcase(branch, p[1:]):
                selected = False
        elif fnmatch.fnmatchcase(branch, p):
            selected = True
    return selected


def _pr_problems(check: str, filters: list[dict], default_branch: str, mg_filters: list[dict] = ()) -> list[str]:
    out = []
    for f in mg_filters:
        types = _as_list(f.get("types"))
        if types and "checks_requested" not in types:
            out.append(f"required check {check!r} runs on merge_group types {types} without checks_requested")
        branches, ignore = _as_list(f.get("branches")), _as_list(f.get("branches-ignore"))
        if branches and not _branch_selected(default_branch, branches):
            out.append(f"required check {check!r} runs on merge_group only for branches {branches}, "
                       f"never for the {default_branch} queue")
        if any(fnmatch.fnmatchcase(default_branch, b) for b in ignore):
            out.append(f"required check {check!r} ignores the {default_branch} queue (merge_group branches-ignore)")
    for f in filters:
        branches, ignore = _as_list(f.get("branches")), _as_list(f.get("branches-ignore"))
        if branches and not _branch_selected(default_branch, branches):
            out.append(f"required check {check!r} runs on pull_request only for branches {branches}, "
                       f"never for PRs into {default_branch}")
        if any(fnmatch.fnmatchcase(default_branch, b) for b in ignore):
            out.append(f"required check {check!r} ignores pull_request into {default_branch} (branches-ignore)")
        types = _as_list(f.get("types"))
        if types and not {"opened", "synchronize"} <= set(types):
            out.append(f"required check {check!r} runs on pull_request types {types}, so a new or "
                       "updated PR may never get it")
    return out


def readiness(plan: RepoPlan, checkout: Path, allow_conditional: tuple[str, ...] = (),
              default_branch: str = "main") -> list[str]:
    """Why applying this repo's rulesets now would block it or let red through; empty means safe.

    A repo with no required check would get a queue that lands anything, so it is not ready.
    GitHub counts a skipped required check as passing, so a required job must not skip: no `if:`
    (unless exactly `always()` or listed in the repo's `allow_conditional`), and no `needs:` without
    exactly `always()`, because a failed dependency skips it. A matrix job, a reusable-workflow job, a
    path-filtered workflow or a pull_request filter that leaves out the default branch may never
    report under the required name, which would wedge the queue.
    """
    if not (checkout / ".git").exists() and not (checkout / ".github").exists():
        return [f"no checkout at {checkout}"]
    jobs = workflow_jobs(checkout)
    if not plan.checks:
        both = sorted(n for n, j in jobs.items() if set(GATE_EVENTS) <= j.events)
        return ["no required checks, so its queue would land red changes"
                + (f"; jobs on both events: {', '.join(both)} (add one to settings)" if both else
                   "; add a presubmit that runs on pull_request and merge_group")]
    out = []
    for c in plan.checks:
        j = jobs.get(c)
        if j is None:
            out.append(f"required check {c!r} is not a job on the default branch (merge the PR that adds it first)")
            continue
        if len(j.workflows) > 1:
            where = ", ".join(sorted(set(j.workflows)))
            out.append(f"required check {c!r} is the name of {len(j.workflows)} jobs ({where}); "
                       "rename one so the name means one thing")
        missing = [e for e in GATE_EVENTS if e not in j.events]
        if missing:
            out.append(f"required check {c!r} does not run on {', '.join(missing)}; the queue would wait forever")
        if j.matrix:
            out.append(f"required check {c!r} is a matrix job; its legs report under other names")
        if j.reusable:
            out.append(f"required check {c!r} calls a reusable workflow, so it reports as '{c} / <job>', "
                       "never under its own name")
        if j.path_filtered:
            out.append(f"required check {c!r} is in a workflow filtered by paths, so it may never report")
        out += _pr_problems(c, j.pr_filters, default_branch, j.mg_filters)
        if j.continue_on_error:
            out.append(f"required check {c!r} has continue-on-error, so it reports success when it fails")
        if j.environment:
            out.append(f"required check {c!r} uses an environment, whose approval or wait can hold the queue "
                       "past its timeout")
        always = _is_always(j.condition)
        if j.needs and always and not j.reads_needs:
            out.append(f"required check {c!r} runs after {', '.join(j.needs)} with `if: always()` but never "
                       "compares needs.<job>.result with success (or checks needs.*.result for failure), so it can "
                       "pass when they fail")
        if j.needs and not always:
            out.append(f"required check {c!r} needs {', '.join(j.needs)} without exactly `if: always()`: a "
                       "failed dependency skips it, and GitHub counts a skipped required check as passing")
        elif j.condition and not always and c not in allow_conditional:
            out.append(f"required check {c!r} has `if: {j.condition}`; when it skips, GitHub counts it as "
                       "passing (list it in allow_conditional with a reason if that skip is intended)")
    return out


def _git(checkout: Path, *args: str) -> str | None:
    try:
        # No user or system git config: an insteadOf rule could point ls-remote at a mirror.
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
        r = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


@dataclass(frozen=True)
class CheckoutState:
    head: str | None           # local HEAD, None when the repo has no commits
    branch: str                # the default branch on the remote ("main" when unknown)
    problems: tuple[str, ...]  # why this checkout cannot be trusted for readiness


def checkout_state(checkout: Path, expected_origin: str | None = None) -> CheckoutState:
    """Is this checkout the remote default branch's current head? Readiness read from a stale or
    wrong-branch clone could approve rules the live repo cannot meet (audit S5), and a repo with no
    commits would have its first push to main refused by qq-main (audit S1)."""
    if not (checkout / ".git").exists():
        return CheckoutState(None, "main", (f"{checkout} is not a git clone; clone each repo fresh "
                                            "(docs/apply-settings.md)",))
    head = _git(checkout, "rev-parse", "--verify", "-q", "HEAD")
    url = _git(checkout, "config", "--get", "remote.origin.url")
    if not url:
        return CheckoutState(head, "main", ("no origin remote, so it cannot be compared with the default branch",))
    if expected_origin and url.removesuffix(".git").rstrip("/") != expected_origin:
        return CheckoutState(head, "main", (f"cloned from {url}, not {expected_origin}; clone the real repo",))
    remote = _git(checkout, "ls-remote", "--symref", url, "HEAD")
    if remote is None:
        return CheckoutState(head, "main", (f"cannot read {url} to confirm the checkout is current",))
    branch, remote_head = "main", None
    for line in remote.splitlines():
        ref, _, name = line.partition("\t")
        if ref.startswith("ref: refs/heads/") and name == "HEAD":
            branch = ref.removeprefix("ref: refs/heads/")
        elif name == "HEAD":
            remote_head = ref
    if head is None and remote_head is None:
        return CheckoutState(head, branch, ("empty repository (no commits yet): qq-main would refuse the push "
                                            "that creates the default branch; push a first commit, then apply",))
    if remote_head is None:
        return CheckoutState(head, branch, (f"{url} reports no default branch head; cannot confirm the checkout",))
    if head is None:
        return CheckoutState(head, branch, ("the checkout has no commits but the repo does: delete it and clone again",))
    problems = []
    local_branch = _git(checkout, "symbolic-ref", "--short", "-q", "HEAD")
    if local_branch != branch:
        problems.append(f"checkout is on {local_branch or 'a detached HEAD'}, not the default branch {branch}")
    if head != remote_head:
        problems.append(f"checkout is at {head[:12]} but {branch} is at {remote_head[:12]}: delete it and clone again")
    if _git(checkout, "status", "--porcelain"):
        problems.append("checkout has local changes: delete it and clone again")
    return CheckoutState(head, branch, tuple(problems))


def org_workflows(s: dict, cfg: dict) -> list[dict]:
    """Each [[org_workflows]] entry with `targets` resolved to repo names."""
    out = []
    known = {r["name"] for r in s["repo"]}
    for w in s.get("org_workflows", []):
        t = w["targets"]
        targets = sorted(r["name"] for r in cfg["repos"]["repo"]) if t == "product" else sorted(_as_list(t))
        unknown = sorted(set(targets) - known)
        if unknown or not targets:
            raise GateError(f"org ruleset {w['ruleset']!r}: targets {unknown or '(none)'} are not repos in settings")
        if w["repository"] not in known:
            raise GateError(f"org ruleset {w['ruleset']!r}: repository {w['repository']!r} is not in settings")
        if "sha" in w and not re.fullmatch(r"[0-9a-f]{40}", str(w["sha"])):
            raise GateError(f"org ruleset {w['ruleset']!r}: sha {w['sha']!r} is not a full 40-hex commit")
        if not str(w["ref"]).startswith("refs/heads/"):
            raise GateError(f"org ruleset {w['ruleset']!r}: ref {w['ref']!r} is not a branch (refs/heads/...)")
        if w.get("pinned", False):
            # A pinned ruleset judges exactly one repo with a file that names it; enabled, it must
            # carry its sha (rollers counts only sha-pinned workflows rules).
            if len(targets) != 1 or f"-{targets[0]}-" not in Path(w["path"]).name:
                raise GateError(f"org ruleset {w['ruleset']!r}: a pinned workflow targets one repo, named in "
                                f"its file name; got targets {targets} and path {w['path']!r}")
            if w.get("enabled", False) and "sha" not in w:
                raise GateError(f"org ruleset {w['ruleset']!r}: pinned and enabled, but no sha")
        out.append({**w, "targets": targets})
    names = [w["ruleset"] for w in out]
    if len(set(names)) != len(names):
        raise GateError(f"org ruleset names repeat: {names}")
    return out


def _cancels(c) -> bool:
    return isinstance(c, dict) and c.get("cancel-in-progress") not in (None, False)


def ruleset_workflow_problems(text: str, max_minutes: int | None = None) -> list[str]:
    """Why GitHub could not run this file as an org ruleset workflow, or a run could block a PR or
    queue entry until someone re-runs it ("Troubleshooting rules"): it must run on merge_group and
    pull_request or pull_request_target, and must not cancel in progress (an expression counts)."""
    import yaml  # parsed only here and when reading workflows
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [f"not valid YAML: {e}"]
    if not isinstance(doc, dict):
        return ["not a workflow"]
    out = []
    events = _events(doc.get("on", doc.get(True)))[0]
    if "merge_group" not in events:
        out.append("does not run on merge_group")
    if not events & {"pull_request", "pull_request_target"}:
        out.append("does not run on pull_request or pull_request_target")
    if _cancels(doc.get("concurrency")):
        out.append("its concurrency has cancel-in-progress, which a ruleset workflow must not use")
    jobs = doc.get("jobs") if isinstance(doc.get("jobs"), dict) else {}
    if not jobs:
        out.append("has no jobs")
    for job_id, job in jobs.items():
        if isinstance(job, dict) and _cancels(job.get("concurrency")):
            out.append(f"job {job_id!r} concurrency has cancel-in-progress, which a ruleset workflow must not use")
        # The queue drops a check that has not reported within gate.toml's admission limit.
        limit = job.get("timeout-minutes", 360) if isinstance(job, dict) else 360
        if max_minutes is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit > max_minutes):
            out.append(f"job {job_id!r} may run {limit} minutes, past the queue's {max_minutes}-minute limit "
                       "(set timeout-minutes)")
    return out


def pinned_workflow_problems(text: str, repository: str, default_branch: str = "main",
                             max_minutes: int | None = None) -> list[str]:
    """Why a pinned org ruleset workflow could pass without judging `repository` (owner/name), on
    top of ruleset_workflow_problems. GitHub counts a skipped job as passing, so the only job-level
    `if:` allowed is the repository guard that keeps the file from running in its source repo, and
    no job or step may continue on error. Step-level `if:` is not judged: what the steps run is
    reviewed in the source repo, whose commit the ruleset pins."""
    import yaml
    out = ruleset_workflow_problems(text, max_minutes)
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return out
    if not isinstance(doc, dict):
        return out
    events, filtered, pr, mg = _events(doc.get("on", doc.get(True)))
    if "pull_request" not in events and "pull_request_target" in events:
        out.append("does not run on pull_request")
    if filtered:
        out.append("is path-filtered")
    out += _pr_problems("the workflow", [pr], default_branch, [mg])
    guard = f"github.repository == '{repository}'"
    for job_id, job in (doc.get("jobs") if isinstance(doc.get("jobs"), dict) else {}).items():
        job = job if isinstance(job, dict) else {}
        cond = str(job.get("if", "")).strip()
        if cond.startswith("${{") and cond.endswith("}}"):
            cond = cond[3:-2].strip()
        if cond and cond != guard:
            out.append(f"job {job_id!r} has `if: {cond}`; only `{guard}` is allowed")
        if "uses" in job:
            out.append(f"job {job_id!r} calls a reusable workflow")
        if job.get("continue-on-error") not in (None, False):
            out.append(f"job {job_id!r} has continue-on-error, so a failure can pass")
        for i, step in enumerate(job.get("steps") or [], 1):
            if isinstance(step, dict) and step.get("continue-on-error") not in (None, False):
                out.append(f"job {job_id!r} step {i} has continue-on-error, so a failure can pass")
    return out


def org_workflow_readiness(w: dict, checkouts: Path, max_minutes: int, owner: str) -> list[str]:
    """Why an enabled org workflow should not be required yet, read from the source repo's fresh
    checkout (at `sha` when pinned): GitHub would not run it, or it could block every PR into its
    targets. apply --org checks the same file again through the API before writing."""
    co = checkouts / w["repository"]
    if not (co / ".git").exists():
        return [f"no checkout of {w['repository']} at {co}"]
    at = w.get("sha", "HEAD")
    if at != "HEAD" and _git(co, "cat-file", "-e", f"{at}^{{commit}}") is None:
        _git(co, "fetch", "--quiet", "--depth", "1", "origin", at)
    text = _git(co, "show", f"{at}:{w['path']}")
    if text is None:
        return [f"{w['path']} is not in {w['repository']} at {at[:12]}"]
    if w.get("pinned"):
        return pinned_workflow_problems(text, f"{owner}/{w['targets'][0]}", max_minutes=max_minutes)
    return ruleset_workflow_problems(text, max_minutes)

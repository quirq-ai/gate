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

import yaml

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


def load_settings(backend: str, path: Path | None = None) -> dict:
    path = path or SETTINGS / f"{backend}.toml"
    if not path.is_file():
        raise GateError(f"no settings for backend {backend!r} ({path})")
    with path.open("rb") as f:
        return tomllib.load(f)


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
        rulesets = mod.rulesets(settings, cfg, checks, **repo_options(r))
        plans.append(RepoPlan(r["name"], r["kind"], checks, tuple(rulesets)))
    return plans


def repo_options(r: dict) -> dict:
    """Per-repo ruleset options from settings/<backend>.toml, type-checked."""
    owners, size = r.get("code_owner_review", False), r.get("queue_group_size", 5)
    if not isinstance(owners, bool):
        raise GateError(f"{r['name']}: code_owner_review must be true or false, not {owners!r}")
    if isinstance(size, bool) or not isinstance(size, int):
        raise GateError(f"{r['name']}: queue_group_size must be an integer, not {size!r}")
    state = r.get("state_branches", [])
    if not isinstance(state, list) or len(set(state)) != len(state) or not all(
            isinstance(b, str) and re.fullmatch(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*", b) and b != "main"
            for b in state):
        raise GateError(f"{r['name']}: state_branches must be distinct plain branch names (no patterns, "
                        f"not main), not {state!r}")
    return {"code_owner_review": owners, "group_size": size, "state_branches": tuple(state)}


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
    reads_needs: bool = False  # the job looks at needs.<job>.result (an always() aggregator must)


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
            j.reads_needs = j.reads_needs or bool(NEEDS_RESULT.search(json.dumps(job, default=str)))
    return jobs


NEEDS_RESULT = re.compile(r"needs\.(\*|[\w-]+)\.result")


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
        always = _is_always(j.condition)
        if j.needs and always and not j.reads_needs:
            out.append(f"required check {c!r} runs after {', '.join(j.needs)} with `if: always()` but never "
                       "reads needs.<job>.result, so it passes even when they fail")
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
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
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
        out.append({**w, "targets": targets})
    names = [w["ruleset"] for w in out]
    if len(set(names)) != len(names):
        raise GateError(f"org ruleset names repeat: {names}")
    return out

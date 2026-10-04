"""GitHub backend: required checks are GitHub Actions jobs; the rule is a repository ruleset.

A generated workflow names its job after the builder ("The job name is the required check"), and
with no job `name:` GitHub reports the check run under the job id. So the required check context is
the builder name, and it must come from the GitHub Actions app, so another app cannot post a fake
green check with the same name.

That does not stop a PR from adding its own workflow with a job of the same name: same app, same
name. So `observe` also requires each check to come from the workflow file infra-config generates
for it, and treats a same-named check from any other workflow as a spoof that fails the verdict.
TODO(expert): GitHub's ruleset rule has the same gap; V0-GAT-03 puts `.github/**` behind owner
review, which narrows it, and the merge queue runs workflows from the merge result.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from qqgate.errors import GateError
from qqgate.required import RequiredSet

GITHUB_ACTIONS_APP_ID = 15368  # the GitHub Actions app; checks from any other app do not count
API = "https://api.github.com"
EVENTS = {"change": "pull_request", "queue": "merge_group"}


def generated_workflow(config_root: Path, repo: str, builder: str) -> Path:
    return Path(config_root) / "generated" / "github" / repo / f"qq-{builder}.yml"


def workflow_path(builder: str) -> str:
    """Where a delivered workflow lives in the product repo (qqcfg deliver)."""
    return f".github/workflows/qq-{builder}.yml"


def check_workflows(config_root: Path, required: RequiredSet) -> list[str]:
    """Every required check must be a job in the workflow infra-config generates for it, triggered
    on pull_request and merge_group. A required check no workflow produces would block every PR."""
    problems = []
    for c in required.checks:
        path = generated_workflow(config_root, required.repo, c.builder)
        if not path.is_file():
            problems.append(f"{c.name}: infra-config generates no workflow for it ({path.name} missing); "
                            "set generate = true on the builder")
            continue
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError as e:
            raise GateError(f"{path}: not valid YAML: {e}") from None
        if not isinstance(doc, dict):
            raise GateError(f"{path}: not a workflow (top level is not a mapping)")
        on = doc.get("on", doc.get(True)) or {}
        if isinstance(on, (str, list)):
            on = {on} if isinstance(on, str) else set(on)  # YAML 1.1 reads a bare `on` key as True
        for trig in c.triggers:
            event = EVENTS.get(trig)
            if event and event not in on:
                problems.append(f"{c.name}: {path.name} does not run on {event}")
        jobs = doc.get("jobs") or {}
        job = jobs.get(c.name)
        if job is None:
            problems.append(f"{c.name}: {path.name} has no job {c.name!r}, so no check by that name reports")
        elif not isinstance(job, dict):
            problems.append(f"{c.name}: job {c.name!r} in {path.name} is not a mapping")
        else:
            if "name" in job and job["name"] != c.name:
                problems.append(f"{c.name}: job {c.name!r} in {path.name} reports as {job['name']!r}")
            # GitHub's rule treats a skipped job as passing; the gate never lets a required job skip.
            for key in ("if", "strategy"):
                if key in job:
                    problems.append(f"{c.name}: job {c.name!r} in {path.name} has `{key}:`, so it can skip "
                                    "or report under another name")
            if any(isinstance(v, dict) and ("paths" in v or "paths-ignore" in v) for v in on.values()
                   if isinstance(on, dict)):
                problems.append(f"{c.name}: {path.name} filters by paths, so the check may never report")
    return problems


def required_checks_rule(required: RequiredSet) -> dict:
    """The `required_status_checks` rule of a repository ruleset (REST: POST /repos/{o}/{r}/rulesets)."""
    return _status_checks_rule(required.names)


def _status_checks_rule(names) -> dict:
    # The merge queue tests the exact merge result, so the branch need not be up to date (strict off).
    return {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": False,
            "do_not_enforce_on_create": False,
            "required_status_checks": [{"context": n, "integration_id": GITHUB_ACTIONS_APP_ID} for n in names],
        },
    }


def rulesets(settings: dict, cfg: dict, checks: tuple[str, ...]) -> list[dict]:
    """V0-ORG-03: the repository rulesets (REST: POST /repos/{o}/{r}/rulesets) for one repo."""
    main, refs = settings["main"], settings["release_refs"]
    method = main["merge_method"].upper()
    if method.lower() != cfg["gate"]["merge_queue"]["merge_method"]:
        raise GateError(f"settings merge_method {main['merge_method']!r} differs from gate.toml's "
                        f"{cfg['gate']['merge_queue']['merge_method']!r}")
    if main["bypass"]:
        raise GateError("settings [main] bypass must stay empty: nobody overrides the gate (policy change)")
    rules = [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {"type": "pull_request", "parameters": {
            "required_approving_review_count": main["required_approvals"],
            "dismiss_stale_reviews_on_push": True,
            "require_code_owner_review": False,
            "require_last_push_approval": False,
            "required_review_thread_resolution": False,
            "allowed_merge_methods": [main["merge_method"]],
        }},
        {"type": "merge_queue", "parameters": {
            # A verdict must arrive within the admission bar's hard limit (gate.toml [admission]).
            "check_response_timeout_minutes": cfg["gate"]["admission"]["max_minutes"],
            "grouping_strategy": "ALLGREEN",
            "max_entries_to_build": 5,
            "max_entries_to_merge": 5,
            "merge_method": method,
            "min_entries_to_merge": 1,
            "min_entries_to_merge_wait_minutes": 5,
        }},
    ]
    if checks:
        rules.append(_status_checks_rule(checks))
    bypass = [{"actor_id": i, "actor_type": "Integration", "bypass_mode": "always"}
              for i in refs["bypass_integration_ids"]]
    lock = [{"type": t} for t in ("creation", "update", "deletion", "non_fast_forward")]
    return [
        {"name": main["ruleset"], "target": "branch", "enforcement": "active", "bypass_actors": [],
         "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}}, "rules": rules},
        {"name": refs["ruleset"] + "-branches", "target": "branch", "enforcement": "active", "bypass_actors": bypass,
         "conditions": {"ref_name": {"include": [f"refs/heads/{b}" for b in refs["branches"]], "exclude": []}},
         "rules": lock},
        {"name": refs["ruleset"] + "-tags", "target": "tag", "enforcement": "active", "bypass_actors": bypass,
         "conditions": {"ref_name": {"include": [f"refs/tags/{t}" for t in refs["tags"]], "exclude": []}},
         "rules": lock},
    ]


def org_ruleset(settings: dict, cfg: dict, repository_id: int) -> dict:
    """The org ruleset that runs infra-config's drift workflow in every product repo (V0-CFG-02).
    `repository_id` is infra-config's numeric GitHub id, looked up at apply time."""
    w = settings["org_workflows"]
    return {
        "name": w["ruleset"], "target": "branch", "enforcement": "active", "bypass_actors": [],
        "conditions": {
            "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
            "repository_name": {"include": sorted(r["name"] for r in cfg["repos"]["repo"]),
                                "exclude": [], "protected": True},
        },
        "rules": [{"type": "workflows", "parameters": {
            "do_not_enforce_on_create": False,
            "workflows": [{"path": w["path"], "repository_id": repository_id, "ref": w["ref"]}],
        }}],
    }


def apply_org(owner: str, settings: dict, cfg: dict, token: str, write: bool) -> list[str]:
    repo_id = _send("GET", f"{API}/repos/{owner}/{settings['org_workflows']['repository']}", token)["id"]
    rs = org_ruleset(settings, cfg, repo_id)
    base = f"{API}/orgs/{owner}/rulesets"
    existing = {r["name"]: r["id"] for r in _send("GET", f"{base}?per_page=100", token)}
    if rs["name"] in existing:
        action, method, url = "update", "PUT", f"{base}/{existing[rs['name']]}"
    else:
        action, method, url = "create", "POST", base
    if write:
        _send(method, url, token, rs)
    return [f"{owner} (org): {action} ruleset {rs['name']}" + ("" if write else " (dry run)")]


def _send(method: str, url: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise GateError(f"GitHub API {method} {url}: {e.code} {e.reason}: {detail}") from None


def existing_protection(owner: str, repo: str, ours: set[str], token: str) -> list[str]:
    """Other rules on the repo that stack with ours. A check they require that never runs on
    merge_group would wedge the queue, so the admin reviews them before writing."""
    out = []
    info = _send("GET", f"{API}/repos/{owner}/{repo}", token)
    branch = info.get("default_branch", "main")
    try:
        prot = _send("GET", f"{API}/repos/{owner}/{repo}/branches/{branch}/protection", token)
        checks = ((prot.get("required_status_checks") or {}).get("contexts")) or []
        out.append(f"classic branch protection on {branch} (required checks: {', '.join(checks) or 'none'}); "
                   "it stacks with the rulesets, so remove it or make sure its checks run on merge_group")
    except GateError as e:
        if " 404 " not in str(e):
            raise
    others = [r["name"] for r in _send("GET", f"{API}/repos/{owner}/{repo}/rulesets?includes_parents=true&per_page=100", token)
              if r["name"] not in ours]
    if others:
        out.append(f"other rulesets apply too: {', '.join(others)}")
    return out


def apply(owner: str, repo: str, wanted: list[dict], token: str, write: bool) -> list[str]:
    """Create or update each wanted ruleset by name; never deletes rulesets it did not create."""
    base = f"{API}/repos/{owner}/{repo}/rulesets"
    existing = {r["name"]: r["id"] for r in _send("GET", f"{base}?includes_parents=false&per_page=100", token)}
    done = []
    for rs in wanted:
        if rs["name"] in existing:
            action, method, url = "update", "PUT", f"{base}/{existing[rs['name']]}"
        else:
            action, method, url = "create", "POST", base
        if write:
            _send(method, url, token, rs)
        done.append(f"{owner}/{repo}: {action} ruleset {rs['name']}" + ("" if write else " (dry run)"))
    return done


def owner_repo(source: str) -> str:
    """'github.com/quirq-ai/xo-space' -> 'quirq-ai/xo-space'."""
    parts = source.removeprefix("https://").strip("/").split("/")
    if len(parts) != 3 or parts[0] != "github.com":
        raise GateError(f"{source!r} is not a github.com/<owner>/<repo> source")
    return f"{parts[1]}/{parts[2]}"


def _get(url: str, token: str | None) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise GateError(f"GitHub API {e.code} for {url}: {e.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GateError(f"GitHub API unreachable for {url}: {e}") from None
    except json.JSONDecodeError as e:
        raise GateError(f"GitHub API returned non-JSON for {url}: {e}") from None


SPOOFED = "spoofed"   # a same-named check from a workflow other than the generated one


def _pages(url: str, key: str, token: str | None) -> list[dict]:
    out, page = [], 1
    while True:
        data = _get(f"{url}{'&' if '?' in url else '?'}per_page=100&page={page}", token)
        items = data.get(key, [])
        out += items
        if not items or page * 100 >= data.get("total_count", 0):
            return out
        page += 1


def observe(source: str, sha: str, expected: dict[str, str], token: str | None = None) -> dict[str, str]:
    """Check name -> conclusion (or status while running) for one commit.

    `expected` maps each required check to the workflow file that must produce it. Only GitHub
    Actions check runs from that workflow count. A same-named run from any other workflow makes the
    check SPOOFED, which is not green. If the right workflow ran more than once, the newest run
    decides; a run that has not started yet counts as newest, so a pending re-run fails closed.
    """
    token = token if token is not None else (os.environ.get("QQ_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    repo = owner_repo(source)
    q = urllib.parse.quote(sha)
    suites = {r["check_suite_id"]: r.get("path", "")
              for r in _pages(f"{API}/repos/{repo}/actions/runs?head_sha={q}", "workflow_runs", token)}
    runs = _pages(f"{API}/repos/{repo}/commits/{q}/check-runs?app_id={GITHUB_ACTIONS_APP_ID}&filter=all",
                  "check_runs", token)
    seen: dict[str, tuple[str, str]] = {}
    spoofed: set[str] = set()
    for run in runs:
        name = run["name"]
        if name not in expected:
            continue
        path = suites.get((run.get("check_suite") or {}).get("id"), "")
        # A workflow run's path may carry a ref suffix ("path@ref"); compare the file only.
        if path.split("@", 1)[0] != expected[name]:
            spoofed.add(name)
            continue
        state = run.get("conclusion") or run.get("status") or "unknown"
        started = run.get("started_at") or "9999"   # not started yet: newest, so it decides
        if name not in seen or started >= seen[name][0]:
            seen[name] = (started, state)
    out = {name: state for name, (_, state) in seen.items()}
    for name in spoofed:
        out[name] = SPOOFED
    return out


# --- V0-GAT-04: queue-entry time of a merge_group run -------------------------------------------

def queue_pr_number(head_ref: str) -> int:
    """The PR a merge-group ref was built for: refs/heads/gh-readonly-queue/main/pr-123-<sha>."""
    import re
    m = re.search(r"gh-readonly-queue/.+/pr-(\d+)-[0-9a-f]+$", head_ref or "")
    if not m:
        raise GateError(f"{head_ref!r} is not a merge-queue ref")
    return int(m.group(1))


def queued_at(event: dict, repository: str, token: str | None = None):
    """When the PR behind a merge_group event entered the queue.

    Exact: the newest `added_to_merge_queue` event on the PR's timeline at or before the group was
    built (a PR removed and re-added is timed from the entry this group came from, even when an old
    group's run is still going after a later re-add). This assumes head_commit.timestamp is when the
    queue made the group commit, which holds for squash and merge queues (settings use squash). Fallback, marked inexact: the merge-group
    commit's timestamp, which is when the queue built the group, so it leaves out waiting before it.
    """
    from qqgate import timing

    mg = event.get("merge_group")
    if not mg:
        raise GateError("not a merge_group event, so it is not a gate run")
    number = queue_pr_number(mg.get("head_ref", ""))
    ts = (mg.get("head_commit") or {}).get("timestamp")
    if token is None:
        token = os.environ.get("QQ_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    try:
        items = _get_list(f"{API}/repos/{repository}/issues/{number}/timeline", token)
        added = sorted(timing.rfc3339(e["created_at"]) for e in items
                       if e.get("event") == "added_to_merge_queue" and e.get("created_at"))
        if added:
            built = timing.rfc3339(ts) if ts else None
            before = [a for a in added if built is None or a <= built]
            if before:  # none at or before the build: this group's entry is unknown, so fall back
                return timing.QueuedAt(before[-1], "timeline:added_to_merge_queue", True)
    except GateError:
        pass  # fall back below; the fallback is marked inexact
    if not ts:
        raise GateError(f"PR #{number}: no added_to_merge_queue event and no head_commit timestamp")
    return timing.QueuedAt(timing.rfc3339(ts), "merge_group.head_commit.timestamp", False)


def _get_list(url: str, token: str | None) -> list[dict]:
    """GET a paged list endpoint that returns a bare JSON array."""
    out, page = [], 1
    while True:
        items = _get(f"{url}{'&' if '?' in url else '?'}per_page=100&page={page}", token)
        if not isinstance(items, list):
            raise GateError(f"{url}: expected a list")
        out += items
        if len(items) < 100:
            return out
        page += 1

"""GitHub backend: required checks are GitHub Actions jobs; the rule is a repository ruleset.

A generated workflow names its job after the builder ("The job name is the required check"), and
with no job `name:` GitHub reports the check run under the job id. So the required check context is
the builder name, and it must come from the GitHub Actions app, so another app cannot post a fake
green check with the same name.

That does not stop a PR from adding its own workflow with a job of the same name: same app, same
name. So `observe` also requires each check to come from the workflow file infra-config generates
for it, and treats a same-named check from any other workflow as a spoof that fails the verdict.
GitHub's ruleset rule has the same gap (audit S6): it matches name and app only. What closes it is an
org "require workflows" ruleset, which runs the workflow from another repo's main (`org_ruleset`;
qq-drift for product repos, promotion-gate for toolchains), plus owner review on `.github/**`
(V0-GAT-03).
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from qqgate.errors import GateError
from qqgate.required import RequiredSet

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The token must only ever reach api.github.com: a redirect is an error, never followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError for the 3xx


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(url: str, token: str | None, method: str = "GET", data: bytes | None = None) -> urllib.request.Request:
    if not url.startswith(API + "/"):
        raise GateError(f"refusing to send a request outside {API}: {url}")
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json"})
    if token:
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    return req


GITHUB_ACTIONS_APP_ID = 15368  # the GitHub Actions app; checks from any other app do not count
API = "https://api.github.com"
EVENTS = {"change": "pull_request", "queue": "merge_group"}


def generated_workflow(config_root: Path, repo: str, builder: str) -> Path:
    return Path(config_root) / "generated" / "github" / repo / f"qq-{builder}.yml"


def generated_jobs(config_root: Path, repo: str, builder: str) -> set[str]:
    """Job names in the workflow infra-config generates for a builder; empty if there is none."""
    import yaml  # here, not at the top: `queued-at` runs with nothing installed (timing action)
    path = generated_workflow(config_root, repo, builder)
    try:
        doc = yaml.safe_load(path.read_text()) if path.is_file() else None
    except (OSError, yaml.YAMLError):
        return set()
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    return {str(k) for k in jobs} if isinstance(jobs, dict) else set()


def workflow_path(builder: str) -> str:
    """Where a delivered workflow lives in the product repo (qqcfg deliver)."""
    return f".github/workflows/qq-{builder}.yml"


def check_workflows(config_root: Path, required: RequiredSet) -> list[str]:
    """Every required check must be a job in the workflow infra-config generates for it, triggered
    on pull_request and merge_group. A required check no workflow produces would block every PR."""
    import yaml  # here, not at the top: `queued-at` runs with nothing installed (timing action)
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


def rulesets(settings: dict, cfg: dict, checks: tuple[str, ...], code_owner_review: bool = False,
             required_approvals: int | None = None, group_size: int = 5, state_branches: tuple[str, ...] = (),
             dependabot_branches: bool = False, release_tags: tuple[str, ...] = (),
             release_executor: bool = False, executor_branches: tuple[str, ...] = ()) -> list[dict]:
    """V0-ORG-03: the repository rulesets (REST: POST /repos/{o}/{r}/rulesets) for one repo."""
    # release_refs is optional only in a user org's settings (load_settings requires it in gate's own).
    main, refs = settings["main"], settings.get("release_refs")
    method = main["merge_method"].upper()
    if method.lower() != cfg["gate"]["merge_queue"]["merge_method"]:
        raise GateError(f"settings merge_method {main['merge_method']!r} differs from gate.toml's "
                        f"{cfg['gate']['merge_queue']['merge_method']!r}")
    if isinstance(group_size, bool) or not isinstance(group_size, int) or not 1 <= group_size <= 5:
        raise GateError(f"queue_group_size {group_size} must be 1 to 5")
    if main["bypass"]:
        raise GateError("settings [main] bypass must stay empty: nobody overrides the gate (policy change)")
    default = main.get("required_approvals")
    if isinstance(default, bool) or not isinstance(default, int) or not 0 <= default <= 10:
        raise GateError(f"settings [main] required_approvals must be an integer from 0 to 10, not {default!r}")
    rules = [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {"type": "pull_request", "parameters": {
            # Per repo (settings required_approvals), else [main]'s.
            "required_approving_review_count": (main["required_approvals"] if required_approvals is None
                                                else required_approvals),
            "dismiss_stale_reviews_on_push": True,
            # Per repo (settings code_owner_review); it only bites for paths CODEOWNERS gives owners.
            "require_code_owner_review": code_owner_review,
            "require_last_push_approval": False,
            "required_review_thread_resolution": False,
            "allowed_merge_methods": [main["merge_method"]],
        }},
        {"type": "merge_queue", "parameters": {
            # A verdict must arrive within the admission bar's hard limit (gate.toml [admission]).
            "check_response_timeout_minutes": cfg["gate"]["admission"]["max_minutes"],
            "grouping_strategy": "ALLGREEN",
            "max_entries_to_build": group_size,
            "max_entries_to_merge": group_size,
            "merge_method": method,
            "min_entries_to_merge": 1,
            "min_entries_to_merge_wait_minutes": 5,
        }},
    ]
    if checks:
        rules.append(_status_checks_rule(checks))
    # The release executor bypasses its refs only where its App is installed (release_executor):
    # GitHub may refuse an Integration bypass for an App not installed on the repo, and the dry run
    # (GET only) could not show that before the write.
    bypass = [{"actor_id": i, "actor_type": "Integration", "bypass_mode": "always"}
              for i in refs["bypass_integration_ids"]] if release_executor and refs else []
    lock = [{"type": t} for t in ("creation", "update", "deletion", "non_fast_forward")]
    state = []
    if state_branches:
        # A tool's own record branches (a ledger, a status feed): its bot keeps pushing to them, but
        # nobody may delete or rewrite them, which would reset what they count.
        state = [{"name": "qq-state-branches", "target": "branch", "enforcement": "active", "bypass_actors": [],
                  "conditions": {"ref_name": {"include": [f"refs/heads/{b}" for b in state_branches],
                                              "exclude": []}},
                  "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}]}]
    if executor_branches:
        # State branches only the release executor may write (release-state: channels.json and
        # operation keys). qq-state-branches still applies on top: rulesets stack, so even the
        # executor cannot delete or force-push them.
        if not bypass:
            raise GateError("executor_branches need the release executor's bypass")
        state.append({"name": refs["state_ruleset"], "target": "branch", "enforcement": "active",
                      "bypass_actors": bypass,
                      "conditions": {"ref_name": {"include": [f"refs/heads/{b}" for b in executor_branches],
                                                  "exclude": []}},
                      "rules": lock})
    # A tag named like a branch (`main`) satisfies a workflow's `github.ref_name == 'main'` test, and
    # wins over a state branch of its name on a short-name `git fetch` (release audit), so nobody may
    # create, move or delete a tag named like `main` or any repo's state branch. (lkgr and channels/**/*
    # tags are the release executor's, in the release-refs ruleset.)
    tags = settings["reserved_tags"]
    names = list(tags["names"]) + sorted({b for r in settings.get("repo", []) for b in r.get("state_branches", ())}
                                         - set(tags["names"]))
    state.append({"name": tags["ruleset"], "target": "tag", "enforcement": "active", "bypass_actors": [],
                  "conditions": {"ref_name": {"include": [f"refs/tags/{t}" for t in names], "exclude": []}},
                  "rules": lock})
    if release_tags:
        # Tags a pin trusts (qq: a version-only pin installs tag v<version>, and a git: digest
        # must be on a branch or tag): nobody but the release executor may create, move or delete
        # them, the same bypass as lkgr and channels/**/*.
        state.append({"name": "qq-release-tags", "target": "tag", "enforcement": "active", "bypass_actors": bypass,
                      "conditions": {"ref_name": {"include": [f"refs/tags/{t}" for t in release_tags],
                                                  "exclude": []}},
                      "rules": lock})
    if dependabot_branches:
        # rollers lands a Dependabot PR only if nobody but Dependabot can change its branch after the
        # check (its land check reads `update` and `non_fast_forward` on the PR branch).
        bot = settings["dependabot"]
        state.append({"name": bot["ruleset"], "target": "branch", "enforcement": "active",
                      "bypass_actors": [{"actor_id": bot["actor_id"], "actor_type": "Integration",
                                         "bypass_mode": "always"}],
                      "conditions": {"ref_name": {"include": [bot["branches"]], "exclude": []}},
                      "rules": [{"type": "update"}, {"type": "non_fast_forward"}]})
    out = [{"name": main["ruleset"], "target": "branch", "enforcement": "active", "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}}, "rules": rules}]
    if refs is None:
        return out + state
    return out + [
        {"name": refs["ruleset"] + "-branches", "target": "branch", "enforcement": "active", "bypass_actors": bypass,
         "conditions": {"ref_name": {"include": [f"refs/heads/{b}" for b in refs["branches"]], "exclude": []}},
         "rules": lock},
        {"name": refs["ruleset"] + "-tags", "target": "tag", "enforcement": "active", "bypass_actors": bypass,
         "conditions": {"ref_name": {"include": [f"refs/tags/{t}" for t in refs["tags"]], "exclude": []}},
         "rules": lock},
    ] + state


def org_ruleset(workflow: dict, targets: list[str], repository_id: int) -> dict:
    """An org ruleset that runs `workflow["path"]` from `workflow["repository"]` at `workflow["ref"]`
    (pinned to `workflow["sha"]` when set) on every PR and queue entry into the default branch of
    `targets`. `repository_id` is the source repo's numeric GitHub id, looked up at apply time."""
    entry = {"path": workflow["path"], "repository_id": repository_id, "ref": workflow["ref"]}
    if "sha" in workflow:
        entry["sha"] = workflow["sha"]
    return {
        "name": workflow["ruleset"], "target": "branch", "enforcement": "active", "bypass_actors": [],
        "conditions": {
            "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []},
            "repository_name": {"include": sorted(targets), "exclude": [], "protected": True},
        },
        "rules": [{"type": "workflows", "parameters": {
            "do_not_enforce_on_create": False,
            "workflows": [entry],
        }}],
    }


def plan_org(owner: str, workflow: dict, targets: list[str], token: str, max_minutes: int) -> dict:
    """Read-only: check the file GitHub would run and compute the org ruleset write. Targets already
    on a live ruleset of this name are kept, so a run limited to some repos never drops the others."""
    src = f"{API}/repos/{owner}/{workflow['repository']}"
    repo_id = _send("GET", src, token)["id"]
    at = workflow["ref"]
    if "sha" in workflow:
        # The pinned commit must already be on the branch (reviewed and merged).
        branch = workflow["ref"].removeprefix("refs/heads/")
        # branch...sha: "behind" or "identical" means sha is an ancestor of the branch (empty diff).
        cmp = _send("GET", f"{src}/compare/{urllib.parse.quote(branch, safe='')}...{workflow['sha']}", token)
        if cmp.get("status") not in ("behind", "identical"):
            raise GateError(f"org ruleset {workflow['ruleset']}: {workflow['sha']} is not on "
                            f"{workflow['repository']} {branch} (compare status {cmp.get('status')!r})")
        at = workflow["sha"]
    # The file GitHub will run must be one it can run without blocking the targets.
    path = urllib.parse.quote(workflow["path"])
    doc = _send("GET", f"{src}/contents/{path}?ref={urllib.parse.quote(at, safe='')}", token)
    if not isinstance(doc, dict) or doc.get("type") != "file" or doc.get("encoding") != "base64":
        raise GateError(f"org ruleset {workflow['ruleset']}: {workflow['path']} at {at} is not a file")
    from qqgate import settings as settings_mod  # settings loads backends; import late
    text = base64.b64decode(doc.get("content", "")).decode("utf-8", errors="replace")
    problems = (settings_mod.pinned_workflow_problems(text, f"{owner}/{targets[0]}", max_minutes=max_minutes)
                if workflow.get("pinned") else settings_mod.ruleset_workflow_problems(text, max_minutes))
    if problems:
        raise GateError(f"org ruleset {workflow['ruleset']}: {workflow['path']} at {at[:12]}: " + "; ".join(problems))
    base = f"{API}/orgs/{owner}/rulesets"
    existing = {r["name"]: r["id"] for r in _send("GET", f"{base}?per_page=100", token)}
    if workflow["ruleset"] in existing:
        url = f"{base}/{existing[workflow['ruleset']]}"
        live = _send("GET", url, token)
        kept = (((live.get("conditions") or {}).get("repository_name") or {}).get("include")) or []
        if workflow.get("pinned") and set(kept) - set(targets):
            raise GateError(f"org ruleset {workflow['ruleset']} is live for {kept}; a pinned ruleset judges only "
                            f"{targets}: fix it in Settings > Rules first")
        rs = org_ruleset(workflow, sorted(set(targets) | set(kept)), repo_id)
        return _change(f"{owner} (org)", rs, url, live)
    return _change(f"{owner} (org)", org_ruleset(workflow, targets, repo_id), base, None)


def _send(method: str, url: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = _request(url, token, method, data)
    try:
        with _OPENER.open(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise GateError(f"GitHub API {method} {url}: {e.code} {e.reason}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GateError(f"GitHub API {method} {url}: unreachable: {e}") from None
    except json.JSONDecodeError as e:
        raise GateError(f"GitHub API {method} {url}: not JSON: {e}") from None


def existing_protection(owner: str, repo: str, ours: set[str], token: str) -> list[str]:
    """Other rules on the repo that stack with ours. A check they require that never runs on
    merge_group would wedge the queue, so the admin reviews them before writing."""
    out = []
    info = _send("GET", f"{API}/repos/{owner}/{repo}", token)
    branch = info.get("default_branch", "main")
    if info.get("allow_squash_merge") is False:
        out.append("squash merging is turned off for this repo, and the merge queue squashes; turn it on "
                   "in Settings > General > Pull Requests")
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


def check_repo(owner: str, repo: str, ours: set[str], approvals: int, token: str) -> list[tuple[str, str]]:
    """`settings check` for one repo, GETs only and whatever its readiness: ("warning" | "ok", line)
    for what already protects it (as existing_protection: squash off, classic protection, other
    rulesets), the reviews they require, its visibility and its Actions permissions. Anything it
    cannot read is a warning, never "none"."""
    out: list[tuple[str, str]] = []
    base = f"{API}/repos/{owner}/{repo}"

    def read(what: str, url: str, absent_on_404: bool = False):
        try:
            return _send("GET", url, token)
        except GateError as e:
            if absent_on_404 and " 404 " in str(e):
                return None
            out.append(("warning", f"could not read {what}: {e}"))
            return False

    info = read("the repo", base)
    branch = "main"
    if isinstance(info, dict):
        branch = info.get("default_branch") or "main"
        visibility = info.get("visibility")
        out.append(("ok", f"visibility {visibility}") if visibility else ("warning", "could not read its visibility"))
        squash = info.get("allow_squash_merge")
        if squash is False:
            out.append(("warning", "squash merging is turned off for this repo, and the merge queue squashes; turn "
                                   "it on in Settings > General > Pull Requests"))
        elif squash is None:
            out.append(("warning", "could not read whether squash merging is on (needs admin on the repo)"))
    b = urllib.parse.quote(branch, safe="")
    prot = read(f"classic branch protection on {branch}", f"{base}/branches/{b}/protection", absent_on_404=True)
    if prot is None:
        out.append(("ok", f"no classic branch protection on {branch}"))
    elif isinstance(prot, dict):
        checks = ((prot.get("required_status_checks") or {}).get("contexts")) or []
        out.append(("warning", f"classic branch protection on {branch} (required checks: {', '.join(checks) or 'none'}); "
                               "it stacks with the rulesets, so remove it or make sure its checks run on merge_group"))
        reviews = prot.get("required_pull_request_reviews")
        if isinstance(reviews, dict):
            out.append(("warning", f"classic branch protection on {branch} requires pull request reviews "
                                   f"({reviews.get('required_approving_review_count', 'count unknown')} approving)"))
    live = read("its rulesets", f"{base}/rulesets?includes_parents=true&per_page=100")
    names = {}
    if isinstance(live, list):
        names = {r.get("id"): r.get("name") for r in live if isinstance(r, dict)}
        others = [str(n) for n in names.values() if n not in ours]
        if others:
            out.append(("warning", f"other rulesets apply too: {', '.join(others)}"))
    rules = read(f"the ruleset rules on {branch}", f"{base}/rules/branches/{b}?per_page=100")
    if isinstance(rules, list):
        for r in rules:
            if not isinstance(r, dict) or r.get("type") != "pull_request":
                continue
            n = (r.get("parameters") or {}).get("required_approving_review_count")
            name = names.get(r.get("ruleset_id"), f"id {r.get('ruleset_id')}")
            if name in ours:
                out.append(("ok", f"ruleset {name} requires {n} approving review(s) on {branch}"))
            elif n != 0:
                out.append(("warning", f"ruleset {name} requires {n} approving review(s) on {branch}"))
    actions = read("its Actions permissions", f"{base}/actions/permissions")
    if isinstance(actions, dict):
        allowed = actions.get("allowed_actions")
        if actions.get("enabled") is not True:
            out.append(("warning", "GitHub Actions is off, so no required check can run and the queue would wait "
                                   "forever"))
        elif allowed != "all":
            out.append(("warning", f"Actions allows only {allowed or 'unknown'} actions; the generated workflows use "
                                   "actions from other repos, so check they are allowed"))
        else:
            out.append(("ok", "Actions on, all actions allowed"))
    wf = read("its Actions workflow permissions", f"{base}/actions/permissions/workflow")
    if isinstance(wf, dict):
        approve = wf.get("can_approve_pull_request_reviews")
        out.append(("ok", f"workflow token {wf.get('default_workflow_permissions')}, can approve pull requests: "
                          f"{approve}"))
        if approve is not False and approvals > 0:
            out.append(("warning", "GitHub Actions can approve pull requests, so a workflow could approve its own PR; "
                                   "turn it off in Settings > Actions > General > Workflow permissions"))
    return out


def plan_repo(owner: str, repo: str, wanted: list[dict], token: str) -> list[dict]:
    """Read-only: what writing each wanted ruleset would do (create, update with what differs, or
    nothing). Rulesets are matched by name; one that differs from ours (a hand-made ruleset of that
    name, or a bypass added in the UI) is an update the admin must allow with --overwrite."""
    base = f"{API}/repos/{owner}/{repo}/rulesets"
    existing = {r["name"]: r["id"] for r in _send("GET", f"{base}?includes_parents=false&per_page=100", token)}
    out = []
    for rs in wanted:
        if rs["name"] in existing:
            url = f"{base}/{existing[rs['name']]}"
            out.append(_change(f"{owner}/{repo}", rs, url, _send("GET", url, token)))
        else:
            out.append(_change(f"{owner}/{repo}", rs, base, None))
    return out


def plan_repo_settings(owner: str, repo: str, wanted: dict, token: str) -> list[dict]:
    """Read-only: what PATCHing the repo's settings (allow_auto_merge) would change, one per setting."""
    if not wanted:
        return []
    url = f"{API}/repos/{owner}/{repo}"
    live = _send("GET", url, token)
    return [{"where": f"{owner}/{repo}", "name": k, "kind": "setting",
             "action": "unchanged" if live.get(k) == v else "update", "method": "PATCH", "url": url,
             "body": {k: v}, "diff": [], "now": live.get(k)} for k, v in sorted(wanted.items())]


def _change(where: str, rs: dict, url: str, live: dict | None) -> dict:
    if live is None:
        return {"where": where, "name": rs["name"], "action": "create", "method": "POST", "url": url, "body": rs,
                "diff": []}
    diff = _diff(live, rs)
    return {"where": where, "name": rs["name"], "action": "update" if diff else "unchanged", "method": "PUT",
            "url": url, "body": rs, "diff": diff}


def _diff(live, ours, path: str = "") -> list[str]:
    """Where the live ruleset differs from ours. Fields GitHub adds that we never send are ignored;
    rules are matched by type, other lists must be equal (a bypass actor added in the UI shows)."""
    if isinstance(ours, dict):
        if not isinstance(live, dict):
            return [path or "(ruleset)"]
        return [d for k, v in ours.items() for d in _diff(live.get(k), v, f"{path}.{k}" if path else k)]
    if isinstance(ours, list):
        if not isinstance(live, list):
            return [path]
        if ours and all(isinstance(r, dict) and "type" in r for r in ours):
            mine = {r["type"]: r for r in ours}
            theirs = {r.get("type"): r for r in live if isinstance(r, dict)}
            if set(mine) != set(theirs):
                return [f"{path} ({', '.join(sorted(set(mine) ^ set(map(str, theirs))))})"]
            return [d for t in mine for d in _diff(theirs[t], mine[t], f"{path}[{t}]")]
        key = lambda x: json.dumps(x, sort_keys=True)  # noqa: E731
        if len(live) != len(ours) or sorted(map(key, ours)) != sorted(map(key, live)):
            if all(isinstance(x, dict) for x in ours + live) and len(live) == len(ours):
                pairs = zip(sorted(live, key=key), sorted(ours, key=key))
                if all(not _diff(a, b) for a, b in pairs):
                    return []
            return [path]
        return []
    return [] if live == ours else [path]


def write(change: dict, token: str) -> str:
    """Send one planned create or update; returns the line to print once it is live."""
    _send(change["method"], change["url"], token, change["body"])
    if change.get("kind") == "setting":
        return f"{change['where']}: {change['action']} setting {change['name']} = {json.dumps(change['body'][change['name']])}"
    return f"{change['where']}: {change['action']} ruleset {change['name']}"


def owner_repo(source: str) -> str:
    """'github.com/quirq-ai/xo-space' -> 'quirq-ai/xo-space'."""
    parts = source.removeprefix("https://").strip("/").split("/")
    if len(parts) != 3 or parts[0] != "github.com":
        raise GateError(f"{source!r} is not a github.com/<owner>/<repo> source")
    return f"{parts[1]}/{parts[2]}"


def _get(url: str, token: str | None) -> dict:
    req = _request(url, token)
    try:
        with _OPENER.open(req, timeout=30) as r:
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

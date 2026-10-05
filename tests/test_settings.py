import argparse
import base64
import copy
import dataclasses
import json
import re
import subprocess

import pytest

from qqgate import settings
from qqgate.backends import github
from qqgate import cli as _cli
from qqgate.cli import main

REAL_PLAN = _cli._plan_without_token
from qqgate.errors import GateError


@pytest.fixture
def s():
    return copy.deepcopy(settings.load_settings("github"))


def plans_by_name(s, cfg, config_root):
    return {p.name: p for p in settings.build(s, cfg, config_root)}


def test_every_infra_and_product_repo_is_covered(s, cfg, config_root):
    plans = plans_by_name(s, cfg, config_root)
    assert {r["name"] for r in cfg["org"]["infra_repo"]} | {r["name"] for r in cfg["repos"]["repo"]} == set(plans)


def test_product_checks_come_from_the_gate(s, cfg, config_root):
    plans = plans_by_name(s, cfg, config_root)
    assert plans["xo-space"].checks == ("xo-space-presubmit",)
    assert plans["innernet"].checks == ("innernet-presubmit",)


def test_main_ruleset_forces_the_queue_and_blocks_direct_push(s, cfg, config_root):
    main_rs = plans_by_name(s, cfg, config_root)["xo-space"].rulesets[0]
    types = [r["type"] for r in main_rs["rules"]]
    assert {"pull_request", "merge_queue", "non_fast_forward", "deletion", "required_status_checks"} <= set(types)
    assert main_rs["bypass_actors"] == []
    assert main_rs["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    mq = next(r for r in main_rs["rules"] if r["type"] == "merge_queue")["parameters"]
    assert mq["merge_method"] == "SQUASH"
    assert mq["check_response_timeout_minutes"] == cfg["gate"]["admission"]["max_minutes"]


def test_release_refs_are_locked_to_the_release_executor(s, cfg, config_root):
    _, branches, tags = plans_by_name(s, cfg, config_root)["gate"].rulesets[:3]
    assert branches["conditions"]["ref_name"]["include"] == ["refs/heads/lkgr", "refs/heads/channels/**/*"]
    assert tags["target"] == "tag"
    for rs in (branches, tags):
        assert {r["type"] for r in rs["rules"]} == {"creation", "update", "deletion", "non_fast_forward"}
        assert rs["bypass_actors"] == []   # until suraj names the release executor


def test_repo_without_checks_is_not_ready(s, cfg, config_root, tmp_path):
    """S2: a queue with no required check would land a red PR."""
    plan = dataclasses.replace(plans_by_name(s, cfg, config_root)["installer"], checks=())
    co = _checkout(tmp_path, "i", "on: [pull_request, merge_group]\njobs:\n  presubmit:\n    runs-on: x\n")
    why = settings.readiness(plan, co)
    assert "no required checks" in why[0] and "jobs on both events: presubmit" in why[0]


def test_infra_repos_with_a_presubmit_list_it(s, cfg, config_root):
    plans = plans_by_name(s, cfg, config_root)
    assert plans["gardener"].checks == ("presubmit",)
    assert plans["rollers"].checks == ("test",)
    assert plans["release"].checks == ("presubmit",)
    assert plans["installer"].checks == ("presubmit",)


def test_website_is_onboarded_like_innernet(s, cfg, config_root):
    plans = plans_by_name(s, cfg, config_root)
    assert plans["website"].checks == ("website-presubmit",)
    web, inn = plans["website"].rulesets, plans["innernet"].rulesets
    assert json.dumps(web).replace("website-presubmit", "X") == json.dumps(inn).replace("innernet-presubmit", "X")
    assert settings.repo_settings(next(r for r in s["repo"] if r["name"] == "website")) == {"allow_auto_merge": True}


def test_code_owner_review_is_per_repo(s, cfg, config_root):
    plans = plans_by_name(s, cfg, config_root)

    def owners(name):
        pr = next(r for r in plans[name].rulesets[0]["rules"] if r["type"] == "pull_request")
        return pr["parameters"]["require_code_owner_review"]
    assert owners("toolchains") is True and owners("sync") is False


def test_toolchains_queue_merges_one_pr_per_group(s, cfg, config_root):
    plans = plans_by_name(s, cfg, config_root)

    def mq(name):
        return next(r for r in plans[name].rulesets[0]["rules"] if r["type"] == "merge_queue")["parameters"]
    assert (mq("toolchains")["max_entries_to_build"], mq("toolchains")["max_entries_to_merge"]) == (1, 1)
    assert mq("sync")["max_entries_to_merge"] == 5


def test_bypass_on_main_is_refused(s, cfg, config_root):
    s["main"]["bypass"] = [{"actor_id": 1}]
    with pytest.raises(GateError, match="nobody overrides the gate"):
        settings.build(s, cfg, config_root)


def test_merge_method_must_match_gate_toml(s, cfg, config_root):
    s["main"]["merge_method"] = "merge"
    with pytest.raises(GateError, match="differs from gate.toml"):
        settings.build(s, cfg, config_root)


def test_missing_or_unknown_repo_is_refused(s, cfg, config_root):
    s["repo"] = [r for r in s["repo"] if r["name"] != "perf"] + [{"name": "nope", "kind": "infra"}]
    with pytest.raises(GateError, match=r"missing \['perf'\], unknown \['nope'\]"):
        settings.build(s, cfg, config_root)


def _checkout(tmp_path, name, workflow):
    wf = tmp_path / name / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(workflow)
    return tmp_path / name


def test_readiness(s, cfg, config_root, tmp_path):
    plan = plans_by_name(s, cfg, config_root)["sync"]
    ok = _checkout(tmp_path, "a", "on:\n  pull_request:\n  merge_group:\njobs:\n  test:\n    runs-on: x\n")
    assert settings.readiness(plan, ok) == []
    no_queue = _checkout(tmp_path, "b", "on: [pull_request]\njobs:\n  test:\n    runs-on: x\n")
    assert "merge_group" in settings.readiness(plan, no_queue)[0]
    renamed = _checkout(tmp_path, "c", "on: [pull_request, merge_group]\njobs:\n  test:\n    name: unit\n")
    assert "not a job" in settings.readiness(plan, renamed)[0]


def test_plan_repo_creates_updates_or_leaves_alone_by_name(monkeypatch):
    calls = []
    live = {"qq-main": {"id": 7, "name": "qq-main", "enforcement": "disabled", "created_by": "x"},
            "qq-same": {"id": 8, "name": "qq-same", "enforcement": "active", "node_id": "n"}}

    def fake(method, url, token, body=None):
        calls.append((method, url))
        if method != "GET":
            return {}
        if url.endswith("per_page=100"):
            return [{"name": n, "id": r["id"]} for n, r in live.items()]
        return next(r for r in live.values() if url.endswith(f"/{r['id']}"))

    monkeypatch.setattr(github, "_send", fake)
    wanted = [{"name": "qq-main", "enforcement": "active"}, {"name": "qq-same", "enforcement": "active"},
              {"name": "qq-release-refs-branches"}]
    changes = github.plan_repo("quirq-ai", "gate", wanted, "t")
    assert [(c["action"], c["diff"]) for c in changes] == [("update", ["enforcement"]), ("unchanged", []),
                                                           ("create", [])]
    assert {m for m, _ in calls} == {"GET"}
    for c in changes:
        if c["action"] != "unchanged":
            github.write(c, "t")
    assert calls[-2:] == [("PUT", "https://api.github.com/repos/quirq-ai/gate/rulesets/7"),
                          ("POST", "https://api.github.com/repos/quirq-ai/gate/rulesets")]


def test_diff_ignores_fields_github_adds_but_sees_a_ui_bypass():
    ours = {"name": "qq-main", "bypass_actors": [], "rules": [
        {"type": "deletion"}, {"type": "pull_request", "parameters": {"require_code_owner_review": False}}]}
    live = {"id": 1, "name": "qq-main", "bypass_actors": [], "_links": {}, "rules": [
        {"type": "pull_request", "parameters": {"require_code_owner_review": False, "required_reviewers": []}},
        {"type": "deletion"}]}
    assert github._diff(live, ours) == []
    live["bypass_actors"] = [{"actor_id": 5, "actor_type": "OrganizationAdmin", "bypass_mode": "always"}]
    live["rules"][0]["parameters"]["require_code_owner_review"] = True
    assert github._diff(live, ours) == ["bypass_actors", "rules[pull_request].parameters.require_code_owner_review"]
    live["rules"].append({"type": "creation"})
    assert "rules (creation)" in github._diff(live, ours)


def test_apply_without_a_gh_token_is_a_clear_error(apply_env, config_root, tmp_path, capsys):
    apply_env.delenv("QQ_GITHUB_TOKEN", raising=False)
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text("#!/bin/sh\necho 'not logged in' >&2\nexit 1\n")
    gh.chmod(0o755)
    apply_env.setenv("PATH", f"{gh.parent}:/usr/bin:/bin")
    assert main(_apply_args(config_root, tmp_path, "sync")) == 2
    assert "gh auth login" in capsys.readouterr().err


def test_token_is_read_only_after_the_plan_child_exits(apply_env, config_root, tmp_path, capsys):
    """Re-check SF-6: a same-user child can read its parent's environment, so the token is fetched
    from gh after the child has exited, never held while infra-config's code runs."""
    from qqgate import cli
    order = []
    apply_env.delenv("QQ_GITHUB_TOKEN", raising=False)
    apply_env.setattr(cli, "_plan_without_token", lambda args: order.append("child") or _child_plan(args))
    real_run = subprocess.run

    def run(cmd, **k):
        if cmd[0] != "gh":
            return real_run(cmd, **k)
        order.append("gh")
        return subprocess.CompletedProcess(cmd, 0, "tok\n", "")
    apply_env.setattr(cli.subprocess, "run", run)
    apply_env.setattr(github, "plan_repo", lambda owner, repo, wanted, token: order.append(token) or [])
    assert main(_apply_args(config_root, tmp_path, "sync")) == 0
    assert order == ["child", "gh", "tok"]


def test_token_in_the_environment_is_warned_about(apply_env, config_root, tmp_path, capsys):
    apply_env.setattr(github, "plan_repo", lambda *a: [])
    assert main(_apply_args(config_root, tmp_path, "sync")) == 0
    assert "WARNING  QQ_GITHUB_TOKEN was in this process's environment" in capsys.readouterr().out


def test_plan_prints_json(config_root, capsys):
    assert main(["settings", "plan", "--config", str(config_root), "--repo", "gate"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["(backend)"] == "github" and out["gate"]["rulesets"][0]["name"] == "qq-main"
    assert {w["ruleset"] for w in out["(org)"]} == {"qq-drift", "qq-toolchains-promotion-gate",
                                                    "qq-xo-space-presubmit-pinned", "qq-innernet-presubmit-pinned",
                                                    "qq-website-presubmit-pinned"}


def test_org_rulesets_run_a_workflow_from_another_repos_main(s, cfg):
    org = {w["ruleset"]: w for w in settings.org_workflows(s, cfg)}
    assert org["qq-drift"]["targets"] == ["innernet", "website", "xo-space"] and org["qq-drift"]["enabled"] is False
    tc = org["qq-toolchains-promotion-gate"]
    assert tc["targets"] == ["toolchains"] and tc["enabled"] is False   # Free plan (suraj, 2026-10-04)
    rs = github.org_ruleset(tc, ["toolchains"], repository_id=42)
    assert rs["conditions"]["repository_name"]["include"] == ["toolchains"]
    assert rs["rules"][0]["parameters"]["workflows"][0] == {
        "path": ".github/workflows/promotion-gate.yml", "repository_id": 42, "ref": "refs/heads/main",
        "sha": tc["sha"]}   # re-check D-2: the file GitHub runs is pinned too


def test_pinned_org_workflow_carries_its_sha(s, cfg):
    """rollers R-2: a workflows rule counts only when every entry carries a 40-hex sha."""
    org = {w["ruleset"]: w for w in settings.org_workflows(s, cfg)}
    for repo in ("xo-space", "innernet"):
        w = org[f"qq-{repo}-presubmit-pinned"]
        assert w["targets"] == [repo] and w["repository"] == "infra-config" and repo in w["path"]
        rs = github.org_ruleset({**w, "sha": "a" * 40}, [repo], repository_id=7)
        assert rs["rules"][0]["parameters"]["workflows"][0]["sha"] == "a" * 40
    drift = {k: v for k, v in org["qq-drift"].items() if k != "sha"}
    assert "sha" not in github.org_ruleset(drift, ["xo-space"], 1)["rules"][0]["parameters"]["workflows"][0]


@pytest.mark.parametrize("field,value,why", [
    ("sha", "abc123", "40-hex"), ("sha", "A" * 40, "40-hex"), ("ref", "main", "not a branch")])
def test_org_workflow_pins_are_checked(s, cfg, field, value, why):
    s["org_workflows"][2][field] = value
    with pytest.raises(GateError, match=why):
        settings.org_workflows(s, cfg)


GOOD_PINNED = """name: qq required xo-space-presubmit
on:
  pull_request:
    branches: [main]
  merge_group:
jobs:
  xo-space-presubmit-pinned:
    if: github.repository == 'quirq-ai/xo-space'
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps: [{run: "true"}]
"""


def _fake_github(monkeypatch, status="behind", text=GOOD_PINNED, kind="file"):
    seen = []

    def send(method, url, token, body=None):
        seen.append((method, url))
        if "/compare/" in url:
            return {"status": status}
        if "/contents/" in url:
            return {"type": kind, "encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        return [] if url.endswith("rulesets?per_page=100") else {"id": 9}
    monkeypatch.setattr(github, "_send", send)
    return seen


PINNED = {"ruleset": "r", "repository": "infra-config", "path": ".github/workflows/qq-required-xo-space-presubmit.yml",
          "ref": "refs/heads/main", "sha": "b" * 40, "pinned": True}


@pytest.mark.parametrize("status,ok", [("behind", True), ("identical", True), ("ahead", False), ("diverged", False)])
def test_apply_org_refuses_a_sha_that_is_not_on_the_branch(monkeypatch, status, ok):
    seen = _fake_github(monkeypatch, status)
    if ok:
        assert github.plan_org("quirq-ai", PINNED, ["xo-space"], "t", 40)["action"] == "create"
        urls = [u for _, u in seen]
        assert f"{github.API}/repos/quirq-ai/infra-config/compare/main...{'b' * 40}" in urls
        assert any(u.endswith(f"/contents/{PINNED['path']}?ref={'b' * 40}") for u in urls)
        assert all(m == "GET" for m, _ in seen)
    else:
        with pytest.raises(GateError, match="is not on infra-config main"):
            github.plan_org("quirq-ai", PINNED, ["xo-space"], "t", 40)


@pytest.mark.parametrize("text,why", [
    (GOOD_PINNED.replace("  merge_group:\n", ""), "does not run on merge_group"),
    (GOOD_PINNED.replace("    branches: [main]", "    paths: [src/**]"), "path-filtered"),
    (GOOD_PINNED.replace("quirq-ai/xo-space'", "quirq-ai/innernet'"), "only `github.repository == 'quirq-ai/xo-space'`"),
    (GOOD_PINNED.replace("    runs-on:", "    continue-on-error: true\n    runs-on:"), "continue-on-error"),
    (GOOD_PINNED.replace("    branches: [main]", "    branches: [dev]"), "never for PRs into main"),
    (GOOD_PINNED.replace('steps: [{run: "true"}]', 'steps: [{run: "true", continue-on-error: true}]'), "step 1"),
    (GOOD_PINNED.replace("jobs:", "concurrency: {group: g, cancel-in-progress: true}\njobs:"), "cancel-in-progress"),
    (GOOD_PINNED.replace("    runs-on:", "    concurrency: {group: g, cancel-in-progress: true}\n    runs-on:"),
     "cancel-in-progress"),
    ("jobs: {}\n", "does not run on pull_request"),
])
def test_apply_org_refuses_a_pinned_workflow_that_can_pass_without_judging(monkeypatch, text, why):
    _fake_github(monkeypatch, text=text)
    with pytest.raises(GateError, match=re.escape(why)):
        github.plan_org("quirq-ai", PINNED, ["xo-space"], "t", 40)


def test_apply_org_checks_an_unpinned_file_at_its_branch(monkeypatch):
    """Every enabled org workflow is read before it is required: GitHub says a ruleset workflow must
    not cancel in progress (toolchains' promotion-gate.yml does, for pull_request_target)."""
    w = {"ruleset": "t", "repository": "toolchains", "path": ".github/workflows/promotion-gate.yml",
         "ref": "refs/heads/main"}
    gate_yml = ("on:\n  pull_request_target:\n  merge_group:\nconcurrency:\n  group: g\n"
                "  cancel-in-progress: ${{ github.event_name == 'pull_request_target' }}\n"
                "jobs:\n  gate: {runs-on: x, timeout-minutes: 40}\n")
    seen = _fake_github(monkeypatch, text=gate_yml)
    with pytest.raises(GateError, match="cancel-in-progress"):
        github.plan_org("quirq-ai", w, ["toolchains"], "t", 40)
    assert any(u.endswith("/contents/.github/workflows/promotion-gate.yml?ref=refs%2Fheads%2Fmain") for _, u in seen)
    _fake_github(monkeypatch, text=gate_yml.replace("  cancel-in-progress: ${{ github.event_name == 'pull_request_target' }}\n", ""))
    assert github.plan_org("quirq-ai", w, ["toolchains"], "t", 40)["action"] == "create"


def test_apply_org_refuses_a_pinned_path_that_is_not_a_file(monkeypatch):
    _fake_github(monkeypatch, kind="dir")
    with pytest.raises(GateError, match="is not a file"):
        github.plan_org("quirq-ai", PINNED, ["xo-space"], "t", 40)


def test_pinned_entries_point_at_a_sha(s, cfg):
    org = {w["ruleset"]: w for w in settings.org_workflows(s, cfg)}
    for repo in ("xo-space", "innernet"):
        w = org[f"qq-{repo}-presubmit-pinned"]
        assert w["pinned"] and re.fullmatch(r"[0-9a-f]{40}", w["sha"])


def test_pinned_entries_need_one_named_target_and_a_sha_to_enable(s, cfg):
    w = s["org_workflows"][2]
    for change, why in (({"targets": ["xo-space", "innernet"]}, "targets one repo"),
                        ({"targets": ["innernet"]}, "targets one repo"),
                        ({"enabled": True, "sha": None}, "no sha")):
        bad = copy.deepcopy(s)
        bad["org_workflows"][2] = {k: v for k, v in {**w, **change}.items() if v is not None}
        with pytest.raises(GateError, match=why):
            settings.org_workflows(bad, cfg)
    ok = copy.deepcopy(s)
    ok["org_workflows"][2] = {**w, "enabled": True, "sha": "c" * 40}
    assert settings.org_workflows(ok, cfg)[2]["sha"] == "c" * 40


def test_state_branches_cannot_be_deleted_or_rewritten(s, cfg, config_root):
    """gardener audit: deleting `ledger` resets the revert cap. The bots still push (no update rule)."""
    plans = plans_by_name(s, cfg, config_root)
    for repo, branches in (("gardener", ["ledger", "tree-status"]), ("release", ["release-state"])):
        rs = {r["name"]: r for r in plans[repo].rulesets}["qq-state-branches"]
        assert rs["conditions"]["ref_name"]["include"] == [f"refs/heads/{b}" for b in branches]
        assert rs["rules"] == [{"type": "deletion"}, {"type": "non_fast_forward"}] and rs["bypass_actors"] == []
    assert "qq-state-branches" not in {r["name"] for r in plans["sync"].rulesets}


@pytest.mark.parametrize("bad", [["main"], ["a*"], ["x", "x"], "ledger", [""], ["HEAD"], ["refs/heads/x"]])
def test_state_branches_are_plain_names(bad):
    with pytest.raises(GateError, match="state_branches"):
        settings.repo_options({"name": "r", "state_branches": bad})


def test_org_targets_must_be_known_repos(s, cfg):
    s["org_workflows"][1]["targets"] = ["nope"]
    with pytest.raises(GateError, match="not repos in settings"):
        settings.org_workflows(s, cfg)


@pytest.mark.parametrize("job,why", [
    ("  test:\n    needs: build\n    runs-on: x\n", "without exactly `if: always()`"),
    ("  test:\n    if: github.actor != 'x'\n    runs-on: x\n", "counts it as passing"),
    ("  test:\n    strategy:\n      matrix:\n        a: [1, 2]\n", "matrix job"),
])
def test_required_jobs_that_can_skip_or_rename_are_not_ready(s, cfg, config_root, tmp_path, job, why):
    plan = plans_by_name(s, cfg, config_root)["sync"]
    co = _checkout(tmp_path, "x", "on: [pull_request, merge_group]\njobs:\n  build:\n    runs-on: x\n" + job)
    assert any(why in w for w in settings.readiness(plan, co))


def test_always_aggregator_and_allowed_condition_are_ready(s, cfg, config_root, tmp_path):
    plan = plans_by_name(s, cfg, config_root)["sync"]
    co = _checkout(tmp_path, "x", "on: [pull_request, merge_group]\njobs:\n  b:\n    runs-on: x\n"
                   "  test:\n    needs: b\n    if: always()\n    runs-on: x\n"
                   "    steps:\n      - run: test \"${{ needs.b.result }}\" = success\n")
    assert settings.readiness(plan, co) == []
    co2 = _checkout(tmp_path, "y", "on: [pull_request, merge_group]\njobs:\n  test:\n    if: false\n")
    assert settings.readiness(plan, co2, allow_conditional=("test",)) == []


def test_path_filtered_and_duplicate_names_are_not_ready(s, cfg, config_root, tmp_path):
    plan = plans_by_name(s, cfg, config_root)["sync"]
    co = _checkout(tmp_path, "x", "on:\n  pull_request:\n    paths: [src/**]\n  merge_group:\njobs:\n  test:\n    runs-on: x\n")
    (co / ".github" / "workflows" / "other.yml").write_text("on: [pull_request, merge_group]\njobs:\n  test:\n    runs-on: x\n")
    whys = settings.readiness(plan, co)
    assert any("paths" in w for w in whys) and any("is the name of 2 jobs" in w for w in whys)


def test_existing_protection_is_reported(monkeypatch):
    def fake(method, url, token, body=None):
        if url.endswith("/protection"):
            return {"required_status_checks": {"contexts": ["old-ci"]}}
        if "/rulesets" in url:
            return [{"name": "qq-main", "id": 1}, {"name": "legacy", "id": 2}]
        return {"default_branch": "main"}
    monkeypatch.setattr(github, "_send", fake)
    lines = github.existing_protection("quirq-ai", "gate", {"qq-main"}, "t")
    assert "old-ci" in lines[0] and "legacy" in lines[1]


def _child_plan(args):
    """What the token-free child prints, computed in-process (no subprocess in these tests)."""
    from qqgate import cli
    cfg, backend, s, plans = cli._plans(args)
    return json.loads(json.dumps(cli._plan_json(cfg, backend, plans, settings.org_workflows(s, cfg))))


@pytest.fixture
def apply_env(monkeypatch, config_root):
    from qqgate import cli
    monkeypatch.setenv("QQ_GITHUB_TOKEN", "t")
    monkeypatch.setattr(settings, "checkout_state",
                        lambda co, origin=None: settings.CheckoutState("a" * 40, "main", ()))
    monkeypatch.setattr(github, "existing_protection", lambda *a: [])
    monkeypatch.setattr(cli, "_plan_without_token", _child_plan)
    monkeypatch.setattr(settings, "org_workflow_readiness", lambda *a: [])
    monkeypatch.setattr(github, "plan_repo_settings", lambda *a: [])
    return monkeypatch


JOB = {"toolchains": ("ci", "promotion-gate"), "sync": ("test",), "gate": ("presubmit",)}


def _apply_args(config_root, tmp_path, *repos, extra=()):
    for r in repos:
        if not (tmp_path / r).exists():
            _checkout(tmp_path, r, "on: [pull_request, merge_group]\njobs:\n"
                      + "".join(f"  {j}:\n    runs-on: x\n" for j in JOB[r]))
    return (["settings", "apply", "--config", str(config_root), "--checkouts", str(tmp_path)]
            + [a for r in repos for a in ("--repo", r)] + list(extra))


def test_org_ruleset_needs_one_enabled(apply_env, config_root, tmp_path, capsys):
    off = copy.deepcopy(settings.load_settings("github"))
    for w in off["org_workflows"]:
        w["enabled"] = False
    real = settings.load_settings
    apply_env.setattr(settings, "load_settings", lambda b, path=None: copy.deepcopy(off) if b == "github" else real(b))
    apply_env.setattr(github, "plan_repo", lambda *a: [])
    assert main(_apply_args(config_root, tmp_path, "sync", extra=["--org"])) == 0
    assert "enabled = false" in capsys.readouterr().err


def test_org_ruleset_targets_only_repos_ready_in_this_run(apply_env, config_root, tmp_path, capsys):
    on = copy.deepcopy(settings.load_settings("github"))
    next(w for w in on["org_workflows"] if w["ruleset"] == "qq-toolchains-promotion-gate")["enabled"] = True
    real = settings.load_settings
    apply_env.setattr(settings, "load_settings", lambda b, path=None: copy.deepcopy(on) if b == "github" else real(b))
    apply_env.setattr(github, "plan_repo", lambda *a: [])
    got = []
    apply_env.setattr(github, "plan_org", lambda owner, wf, targets, token, max_minutes: got.append(targets) or
                      {"where": "o", "name": wf["ruleset"], "action": "unchanged", "diff": []})
    assert main(_apply_args(config_root, tmp_path, "sync", extra=["--org"])) == 0
    assert got == [] and "skip     org ruleset qq-toolchains-promotion-gate" in capsys.readouterr().out
    assert main(_apply_args(config_root, tmp_path, "toolchains", extra=["--org"])) == 0
    assert got == [["toolchains"]]


def test_tampered_child_plan_is_refused(apply_env, config_root, tmp_path, capsys):
    """Review: the token-holding process rebuilds rulesets from settings and refuses a child that
    asks for anything else."""
    from qqgate import cli
    apply_env.setattr(github, "plan_repo", lambda *a: [])
    tampers = [
        lambda d: d["sync"]["rulesets"][0]["bypass_actors"].append({"actor_id": 1}),
        lambda d: d["sync"]["required"].append("extra"),
        lambda d: d.update({"evil": {"kind": "infra", "required": [], "rulesets": []}}),
        lambda d: d["(gate)"].update({"max_minutes": 100000}),
        # a weaker product check, with rulesets to match: refused, it is no generated job
        lambda d: d.update({"xo-space": {**d["xo-space"], "required": ["lint", "tests"], "rulesets": github.rulesets(
            settings.load_settings("github"), {"gate": {"merge_queue": {"merge_method": "squash"},
                                                        "admission": {"max_minutes": d["(gate)"]["max_minutes"]}}},
            ("lint", "tests"))}}),
        lambda d: d.update({"(backend)": "other"}),
        lambda d: d["gardener"].update({"rulesets": [r for r in d["gardener"]["rulesets"]
                                                     if r["name"] != "qq-state-branches"]}),
    ]
    for t in tampers:
        def plan(args, t=t):
            d = _child_plan(argparse.Namespace(**{**vars(args), "repo": ["sync", "xo-space", "gardener"]}))
            t(d)
            return d
        apply_env.setattr(cli, "_plan_without_token", plan)
        assert main(_apply_args(config_root, tmp_path, "sync")) == 2
        assert "plan" in capsys.readouterr().err


def test_child_env_carries_no_credentials(monkeypatch, tmp_path):
    from qqgate import cli
    for k in ("QQ_GITHUB_TOKEN", "GH_TOKEN", "SSH_AUTH_SOCK", "NETRC", "DATABASE_URL", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(k, "x")
    env = cli._child_env(str(tmp_path))
    assert not {"QQ_GITHUB_TOKEN", "GH_TOKEN", "SSH_AUTH_SOCK", "NETRC", "DATABASE_URL",
                "AWS_SECRET_ACCESS_KEY"} & set(env)
    assert env["HOME"] == str(tmp_path) and "PATH" in env


def test_partial_failure_shows_what_is_live(apply_env, config_root, tmp_path, capsys):
    """S3: the second write to sync fails; the first is already live and must be printed."""
    calls = []

    def send(method, url, token, body=None):
        calls.append(method)
        if method == "GET":
            return []
        if calls.count("POST") == 2:
            raise GateError("GitHub API POST: 422 Unprocessable")
        return {}
    apply_env.setattr(github, "_send", send)
    assert main(_apply_args(config_root, tmp_path, "sync", "gate", extra=["--yes"])) == 2
    out = capsys.readouterr().out
    assert "\nquirq-ai/sync: create ruleset qq-main\n" in out
    assert "\nquirq-ai/sync: create ruleset qq-release-refs-branches\n" not in out
    assert "FAILED   quirq-ai/sync qq-release-refs-branches: GitHub API POST: 422" in out
    assert "already live" in out and "Not attempted: quirq-ai/sync qq-release-refs-tags" in out
    assert "quirq-ai/gate qq-main" in out.split("Not attempted:")[1]


def test_plan_is_computed_without_the_token(apply_env, config_root, tmp_path, capsys):
    """S8: the process holding the token never runs infra-config's qqcfg (real child process)."""
    from qqgate import cli, config
    apply_env.setattr(cli, "_plan_without_token", REAL_PLAN)

    def forbidden(*a, **k):
        raise AssertionError("the token-holding process loaded infra-config")
    apply_env.setattr(config, "load", forbidden)
    apply_env.setattr(config, "qqcfg_module", forbidden)
    apply_env.setattr(github, "plan_repo", lambda *a: [{"where": "quirq-ai/sync", "name": "qq-main",
                                                        "action": "create", "diff": []}])
    assert main(_apply_args(config_root, tmp_path, "sync")) == 0
    assert "plan     quirq-ai/sync: create ruleset qq-main" in capsys.readouterr().out


def test_squash_off_is_reported(monkeypatch):
    def fake(method, url, token, body=None):
        if url.endswith("/protection"):
            raise GateError("GitHub API GET x: 404 Not Found: ")
        if "/rulesets" in url:
            return []
        return {"default_branch": "main", "allow_squash_merge": False}
    monkeypatch.setattr(github, "_send", fake)
    assert "squash merging is turned off" in github.existing_protection("quirq-ai", "gate", set(), "t")[0]


@pytest.mark.parametrize("job,why", [
    ("  test:\n    if: always() && github.event_name == 'pull_request'\n    runs-on: x\n", "counts it as passing"),
    ("  test:\n    needs: build\n    if: always() && true\n    runs-on: x\n", "without exactly"),
    ("  test:\n    uses: ./.github/workflows/inner.yml\n", "reusable workflow"),
    ("  test:\n    needs: build\n    if: always()\n    runs-on: x\n", "never compares"),
    ("  test:\n    needs: build\n    if: always()\n    runs-on: x\n    steps:\n      - run: echo ${{ needs.build.outputs.v }}\n",
     "never compares"),
])
def test_audit_s5_holes_are_closed(s, cfg, config_root, tmp_path, job, why):
    plan = plans_by_name(s, cfg, config_root)["sync"]
    co = _checkout(tmp_path, "x", "on: [pull_request, merge_group]\njobs:\n  build:\n    runs-on: x\n" + job)
    assert any(why in w for w in settings.readiness(plan, co))


@pytest.mark.parametrize("pr,why", [
    ("    branches: [development]\n", "never for PRs into main"),
    ("    branches: ['**', '!main']\n", "never for PRs into main"),
    ("    branches-ignore: [main]\n", "ignores pull_request into main"),
    ("    types: [labeled]\n", "types"),
])
def test_pull_request_filters_that_miss_the_default_branch(s, cfg, config_root, tmp_path, pr, why):
    plan = plans_by_name(s, cfg, config_root)["sync"]
    co = _checkout(tmp_path, "x", "on:\n  pull_request:\n" + pr + "  merge_group:\njobs:\n  test:\n    runs-on: x\n")
    assert any(why in w for w in settings.readiness(plan, co))
    ok = _checkout(tmp_path, "y", "on:\n  pull_request:\n    branches: [main]\n  merge_group:\njobs:\n"
                   "  test:\n    if: ${{ always() }}\n    runs-on: x\n")
    assert settings.readiness(plan, ok) == []


def _git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_checkout_state(tmp_path):
    """S1 and S5: empty, stale and wrong-branch checkouts are refused; a fresh clone is fine."""
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", "-b", "main", str(origin))
    empty = tmp_path / "empty"
    _git("clone", "-q", str(origin), str(empty))
    assert "empty repository" in settings.checkout_state(empty).problems[0]

    work = tmp_path / "work"
    _git("clone", "-q", str(origin), str(work))
    for n in ("1", "2"):
        _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", n, cwd=work)
        _git("push", "-q", "origin", "HEAD:main", cwd=work)
        if n == "1":
            stale = tmp_path / "stale"
            _git("clone", "-q", str(origin), str(stale))
    fresh = tmp_path / "fresh"
    _git("clone", "-q", str(origin), str(fresh))
    st = settings.checkout_state(fresh)
    assert st.problems == () and st.branch == "main" and len(st.head) == 40
    assert "delete it and clone again" in settings.checkout_state(stale).problems[0]
    _git("checkout", "-q", "-b", "other", cwd=fresh)
    assert "not the default branch main" in settings.checkout_state(fresh).problems[0]
    assert "not a git clone" in settings.checkout_state(tmp_path / "nope").problems[0]


def test_merge_group_types_must_include_checks_requested(s, cfg, config_root, tmp_path):
    plan = plans_by_name(s, cfg, config_root)["sync"]
    co = _checkout(tmp_path, "x", "on:\n  pull_request:\n  merge_group:\n    types: [destroyed]\njobs:\n"
                   "  test:\n    runs-on: x\n")
    assert any("checks_requested" in w for w in settings.readiness(plan, co))


def test_checkout_must_come_from_the_real_repo(tmp_path):
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git("clone", "-q", str(origin), str(work))
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "1", cwd=work)
    _git("push", "-q", "origin", "HEAD:main", cwd=work)
    st = settings.checkout_state(work, "https://github.com/quirq-ai/work")
    assert "not https://github.com/quirq-ai/work" in st.problems[0]


def test_child_process_sees_no_credentials(tmp_path, monkeypatch):
    """S8, end to end: a stub qqcfg records the environment the real child process runs with."""
    from qqgate import cli
    root = tmp_path / "cfg"
    (root / "tools").mkdir(parents=True)
    seen = tmp_path / "env.json"
    (root / "tools" / "qqcfg.py").write_text(
        "import json, os\n"
        f"json.dump(sorted(os.environ), open({str(seen)!r}, 'w'))\n"
        "raise SystemExit('stub')\n")
    for k in ("QQ_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "NETRC", "DATABASE_URL"):
        monkeypatch.setenv(k, "secret")
    args = argparse.Namespace(config=str(root), repo=None, no_validate=False)
    with pytest.raises(GateError, match="computing the plan failed"):
        cli._plan_without_token(args)
    env = set(json.loads(seen.read_text()))
    assert not env & {"QQ_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "NETRC", "DATABASE_URL"}
    assert "HOME" in env


@pytest.mark.parametrize("job,why", [
    ("  test:\n    runs-on: x\n    continue-on-error: true\n", "continue-on-error"),
    ("  test:\n    runs-on: x\n    continue-on-error: ${{ matrix.x }}\n", "continue-on-error"),
    ("  test:\n    runs-on: x\n    environment: prod\n", "environment"),
])
def test_required_jobs_that_can_pass_red_or_wait_are_not_ready(s, cfg, config_root, tmp_path, job, why):
    """Re-check SF-8: a job that continues on error reports success, and an environment can hold a
    queue entry past the admission limit."""
    plan = plans_by_name(s, cfg, config_root)["sync"]
    co = _checkout(tmp_path, "x", "on: [pull_request, merge_group]\njobs:\n" + job)
    assert any(why in w for w in settings.readiness(plan, co))


@pytest.mark.parametrize("mg,why", [
    ("    branches: [dev]\n", "never for the main queue"),
    ("    branches-ignore: [main]\n", "ignores the main queue"),
])
def test_merge_group_filters_that_miss_the_default_branch(s, cfg, config_root, tmp_path, mg, why):
    plan = plans_by_name(s, cfg, config_root)["sync"]
    co = _checkout(tmp_path, "x", "on:\n  pull_request:\n  merge_group:\n" + mg + "jobs:\n  test:\n    runs-on: x\n")
    assert any(why in w for w in settings.readiness(plan, co))


@pytest.mark.parametrize("limit,ok", [(None, False), (40, True), (45, False), ("${{ inputs.t }}", False)])
def test_org_workflow_jobs_must_finish_inside_the_queue_limit(limit, ok):
    """Re-check SF-4: promotion-gate's 45 minutes outlives the queue's 40, which drops the entry."""
    job = "  gate:\n    runs-on: x\n" + (f"    timeout-minutes: {limit}\n" if limit is not None else "")
    problems = settings.ruleset_workflow_problems("on: [pull_request_target, merge_group]\njobs:\n" + job, 40)
    assert (problems == []) is ok
    if not ok:
        assert "past the queue's 40-minute limit" in problems[0]


def test_org_workflow_readiness_reads_the_pinned_commit(tmp_path):
    """Re-check SF-5: verify reads the org workflow at its sha from the source repo's checkout."""
    co = tmp_path / "infra-config"
    wf = co / ".github" / "workflows"
    wf.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=co)
    path = ".github/workflows/qq-required-xo-space-presubmit.yml"
    (co / path).write_text(GOOD_PINNED)
    _git("add", ".", cwd=co)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "good", cwd=co)
    good = subprocess.run(["git", "rev-parse", "HEAD"], cwd=co, capture_output=True, text=True).stdout.strip()
    (co / path).write_text(GOOD_PINNED.replace("  merge_group:\n", ""))
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "bad", cwd=co)
    w = dict(PINNED, sha=good, targets=["xo-space"])
    assert settings.org_workflow_readiness(w, tmp_path, 40, "quirq-ai") == []
    assert "merge_group" in " ".join(settings.org_workflow_readiness(dict(w, sha="HEAD"), tmp_path, 40, "quirq-ai"))
    assert "not in infra-config" in settings.org_workflow_readiness(dict(w, path="x.yml"), tmp_path, 40, "quirq-ai")[0]
    assert "no checkout" in settings.org_workflow_readiness(w, tmp_path / "none", 40, "quirq-ai")[0]


def test_org_ruleset_keeps_live_targets_it_does_not_name(monkeypatch):
    """Re-check SF-3: a run limited to one repo must not drop the others from a live org ruleset."""
    w = {"ruleset": "t", "repository": "toolchains", "path": ".github/workflows/promotion-gate.yml",
         "ref": "refs/heads/main"}
    gate_yml = "on: [pull_request_target, merge_group]\njobs:\n  gate: {runs-on: x, timeout-minutes: 40}\n"

    def send(method, url, token, body=None):
        if "/compare/" in url:
            return {"status": "behind"}
        if "/contents/" in url:
            body = GOOD_PINNED if "xo-space" in url else gate_yml
            return {"type": "file", "encoding": "base64", "content": base64.b64encode(body.encode()).decode()}
        if url.endswith("rulesets?per_page=100"):
            return [{"name": "t", "id": 3}]
        if url.endswith("/rulesets/3"):
            return {"id": 3, "name": "t", "conditions": {"repository_name": {"include": ["innernet"]}}}
        return {"id": 9}
    monkeypatch.setattr(github, "_send", send)
    c = github.plan_org("quirq-ai", w, ["toolchains"], "t", 40)
    assert c["action"] == "update"
    assert c["body"]["conditions"]["repository_name"]["include"] == ["innernet", "toolchains"]
    with pytest.raises(GateError, match="pinned ruleset judges only"):
        github.plan_org("quirq-ai", dict(PINNED, ruleset="t"), ["xo-space"], "t", 40)


def test_every_repo_reserves_the_main_tag(s, cfg, config_root):
    for plan in plans_by_name(s, cfg, config_root).values():
        rs = {r["name"]: r for r in plan.rulesets}["qq-reserved-tags"]
        assert rs["target"] == "tag" and rs["bypass_actors"] == []
        assert rs["conditions"]["ref_name"]["include"] == [f"refs/tags/{t}" for t in (
            "main", "ledger", "perf-data", "release-state", "results", "tree-status")]
        assert {r["type"] for r in rs["rules"]} == {"creation", "update", "deletion", "non_fast_forward"}


def test_only_dependabot_changes_its_branches_in_product_repos(s, cfg, config_root):
    """Re-check SF-2: rollers lands a Dependabot PR only if its branch has update and non_fast_forward.
    Deferred in settings (re-check D-1); the ruleset is built once a product repo turns it on."""
    plans = plans_by_name(s, cfg, config_root)
    assert all("qq-dependabot-branches" not in {r["name"] for r in p.rulesets} for p in plans.values())
    for r in s["repo"]:
        if r["kind"] == "product":
            r["dependabot_branches"] = True
    plans = plans_by_name(s, cfg, config_root)
    for repo in ("xo-space", "innernet"):
        rs = {r["name"]: r for r in plans[repo].rulesets}["qq-dependabot-branches"]
        assert rs["conditions"]["ref_name"]["include"] == ["refs/heads/dependabot/**/*"]
        assert rs["rules"] == [{"type": "update"}, {"type": "non_fast_forward"}]
        assert rs["bypass_actors"] == [{"actor_id": 29110, "actor_type": "Integration", "bypass_mode": "always"}]
    assert "qq-dependabot-branches" not in {r["name"] for r in plans["sync"].rulesets}


def test_perf_data_cannot_be_deleted_or_rewritten(s, cfg, config_root):
    plans = plans_by_name(s, cfg, config_root)
    for repo, branch in (("perf", "perf-data"), ("test-pipelines", "results")):
        rs = {r["name"]: r for r in plans[repo].rulesets}["qq-state-branches"]
        assert rs["conditions"]["ref_name"]["include"] == [f"refs/heads/{branch}"]


def test_yes_refuses_warnings_and_overwrites_unless_asked(apply_env, config_root, tmp_path, capsys):
    """Re-check SF-7: --yes alone never writes past a WARNING or over a ruleset someone changed."""
    sent = []
    apply_env.setattr(github, "existing_protection", lambda *a: ["branch protection requires 'x'"])
    apply_env.setattr(github, "plan_repo", lambda *a: [
        {"where": "quirq-ai/sync", "name": "qq-main", "action": "update", "diff": ["bypass_actors"]}])
    apply_env.setattr(github, "write", lambda c, t: sent.append(c) or "quirq-ai/sync: update ruleset qq-main")
    assert main(_apply_args(config_root, tmp_path, "sync", extra=["--yes"])) == 1
    out = capsys.readouterr().out
    assert "REFUSED  nothing written" in out and "--accept-warnings" in out and "--overwrite" in out
    assert "plan     quirq-ai/sync: update ruleset qq-main (differs: bypass_actors)" in out and sent == []
    assert main(_apply_args(config_root, tmp_path, "sync", extra=["--yes", "--accept-warnings"])) == 1
    assert "--accept-warnings" not in capsys.readouterr().out.split("REFUSED")[1] and sent == []
    assert main(_apply_args(config_root, tmp_path, "sync", extra=["--yes", "--accept-warnings", "--overwrite"])) == 0
    assert len(sent) == 1


def test_org_entry_whose_file_is_not_ready_is_skipped_and_fails_the_run(apply_env, config_root, tmp_path, capsys):
    on = copy.deepcopy(settings.load_settings("github"))
    next(w for w in on["org_workflows"] if w["ruleset"] == "qq-toolchains-promotion-gate")["enabled"] = True
    real = settings.load_settings
    apply_env.setattr(settings, "load_settings", lambda b, path=None: copy.deepcopy(on) if b == "github" else real(b))
    apply_env.setattr(settings, "org_workflow_readiness", lambda *a: ["has cancel-in-progress"])
    apply_env.setattr(github, "plan_repo", lambda *a: [])
    apply_env.setattr(github, "plan_org", lambda *a: pytest.fail("plan_org called for a skipped entry"))
    assert main(_apply_args(config_root, tmp_path, "toolchains", extra=["--org"])) == 1
    assert "skip     org ruleset qq-toolchains-promotion-gate: has cancel-in-progress" in capsys.readouterr().out


@pytest.mark.parametrize("step,ok", [
    ('test "${{ needs.build.result }}" = success', True),
    ("echo ${{ needs.build.result == 'success' }}", True),
    ("echo ${{ contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled') }}", True),
    ('echo "${{ toJSON(needs.*.result) }}" | jq -e \'all(. == "success")\'', True),
    ("echo ${{ needs.build.result }}", False),
])
def test_always_aggregator_must_judge_its_needs(s, cfg, config_root, tmp_path, step, ok):
    """Review of #13: an aggregator that only looks for failure is judged too (toolchains' ci
    job reads toJSON(needs.*.result) with jq, which no comparison regex sees)."""
    plan = plans_by_name(s, cfg, config_root)["sync"]
    co = _checkout(tmp_path, "x", "on: [pull_request, merge_group]\njobs:\n  build:\n    runs-on: x\n"
                   "  test:\n    needs: build\n    if: always()\n    runs-on: x\n    steps:\n"
                   f"      - run: {json.dumps(step)}\n")
    assert (settings.readiness(plan, co) == []) is ok


def test_verify_runs_infra_config_code_only_in_the_child(apply_env, config_root, tmp_path, capsys):
    """Review of #13: verify, like apply, never loads infra-config in the admin's process."""
    from qqgate import cli, config
    apply_env.setattr(cli, "_plan_without_token", REAL_PLAN)

    def forbidden(*a, **k):
        raise AssertionError("verify loaded infra-config in the admin's process")
    apply_env.setattr(config, "load", forbidden)
    apply_env.setattr(config, "qqcfg_module", forbidden)
    args = _apply_args(config_root, tmp_path, "sync")
    args[1] = "verify"
    assert main(args) == 0
    assert "ready     sync" in capsys.readouterr().out


def test_depot_tags_that_pins_trust_are_locked(s, cfg, config_root):
    """depot pins trust its tags (a version-only pin installs tag v<version>; a git: digest must be
    on a branch or tag), so nobody but the release executor may create, move or delete one."""
    plans = plans_by_name(s, cfg, config_root)
    rs = {r["name"]: r for r in plans["depot"].rulesets}["qq-release-tags"]
    assert rs["target"] == "tag" and rs["conditions"]["ref_name"]["include"] == ["refs/tags/**/*"]
    assert {r["type"] for r in rs["rules"]} == {"creation", "update", "deletion", "non_fast_forward"}
    assert rs["bypass_actors"] == []   # the release executor, once it exists (release_refs)
    assert all("qq-release-tags" not in {r["name"] for r in p.rulesets} for n, p in plans.items() if n != "depot")


@pytest.mark.parametrize("bad", [["refs/tags/v*"], ["v*", "v*"], "v*", ["a b"], ["**"], ["v/**"]])
def test_release_tags_are_plain_patterns(bad):
    with pytest.raises(GateError, match="release_tags"):
        settings.repo_options({"name": "x", "release_tags": bad})


@pytest.mark.parametrize("text, where", [
    ('[release_refs]\nbranches = ["lkgr"]\ntags = ["channels/**"]\n', "release_refs.tags"),
    ('[dependabot]\nbranches = "refs/heads/dependabot/**"\n', "dependabot.branches"),
    ('[reserved_tags]\nnames = ["main", "x/**"]\n', "reserved_tags.names")])
def test_ref_patterns_reach_nested_refs(tmp_path, text, where):
    """GitHub's `**` without a following `/` matches one level only (FNM_PATHNAME)."""
    p = tmp_path / "github.toml"
    p.write_text(text)
    with pytest.raises(GateError, match=where):
        settings.load_settings("github", p)


def test_dependabot_pattern_reaches_its_branches():
    s = settings.load_settings("github")
    assert s["dependabot"]["branches"] == "refs/heads/dependabot/**/*"


def test_yes_writes_only_the_plan_the_dry_run_showed(apply_env, config_root, tmp_path, capsys):
    """Re-check E-2: the write re-plans, so --expect-plan refuses anything the dry run did not show."""
    sent, live = [], {"action": "create", "warn": []}
    saved = tmp_path / "plan.json"
    apply_env.setattr(github, "existing_protection", lambda *a: live["warn"])
    apply_env.setattr(github, "plan_repo", lambda *a: [
        {"where": "quirq-ai/sync", "name": "qq-main", "action": live["action"], "diff": []}])
    apply_env.setattr(github, "write", lambda c, t: sent.append(c) or "quirq-ai/sync: create ruleset qq-main")
    assert main(_apply_args(config_root, tmp_path, "sync", extra=["--save-plan", str(saved)])) == 0
    capsys.readouterr()
    write = ["--yes", "--accept-warnings", "--overwrite", "--expect-plan", str(saved)]
    live["action"] = "update"   # someone changed it on GitHub between the dry run and the write
    assert main(_apply_args(config_root, tmp_path, "sync", extra=write)) == 1
    out = capsys.readouterr().out
    assert "the plan changed since the dry run" in out and "quirq-ai/sync: update qq-main" in out and sent == []
    live.update(action="create", warn=["branch protection requires 'x'"])   # a new WARNING
    assert main(_apply_args(config_root, tmp_path, "sync", extra=write)) == 1
    assert "WARNING sync: branch protection" in capsys.readouterr().out and sent == []
    live["warn"] = []
    assert main(_apply_args(config_root, tmp_path, "sync", extra=write)) == 0
    assert len(sent) == 1


def test_a_repo_that_left_the_plan_only_drops_its_writes(apply_env, config_root, tmp_path, capsys):
    """One repo moving between the dry run and the write does not hold up the others."""
    sent = []
    saved = tmp_path / "plan.json"
    apply_env.setattr(github, "plan_repo", lambda owner, repo, *a: [
        {"where": f"quirq-ai/{repo}", "name": "qq-main", "action": "create", "diff": []}])
    apply_env.setattr(github, "write", lambda c, t: sent.append(c["where"]) or f"{c['where']}: create")
    assert main(_apply_args(config_root, tmp_path, "sync", "gate", extra=["--save-plan", str(saved)])) == 0
    assert main(_apply_args(config_root, tmp_path, "sync", extra=["--yes", "--expect-plan", str(saved)])) == 0
    assert sent == ["quirq-ai/sync"]
    assert main(_apply_args(config_root, tmp_path, "sync", "gate", "toolchains",
                            extra=["--yes", "--expect-plan", str(saved)])) == 1
    assert "quirq-ai/toolchains: create qq-main" in capsys.readouterr().out and sent == ["quirq-ai/sync"]


def test_plan_items_change_with_content():
    """A ruleset whose action, body (an org ruleset's targets) or differences changed is a new item."""
    from qqgate import cli
    c = {"where": "org", "name": "qq-x", "action": "unchanged", "method": "PUT", "url": "u",
         "body": {"conditions": {"repository_id": {"repository_ids": [1]}}}, "diff": []}
    base = set(cli._plan_items([], [c]))
    grown = dict(c, body={"conditions": {"repository_id": {"repository_ids": [1, 2]}}})
    for other in (dict(c, action="update"), grown, dict(c, diff=["rules"])):
        assert not set(cli._plan_items([], [other])) <= base


@pytest.mark.parametrize("text", ['{"' + "a" * 64 + '": 1}', '["x"]', "[1]", "not json"])
def test_expect_plan_must_be_a_saved_plan(apply_env, config_root, tmp_path, capsys, text):
    """Re-check F-2: only a list of hashes from --save-plan is accepted (a JSON object is not)."""
    apply_env.setattr(github, "plan_repo", lambda *a: [])
    saved = tmp_path / "plan.json"
    saved.write_text(text)
    assert main(_apply_args(config_root, tmp_path, "sync", extra=["--yes", "--expect-plan", str(saved)])) == 2
    assert "--expect-plan" in capsys.readouterr().err


def test_allow_auto_merge_is_set_per_repo_and_type_checked(s):
    want = {r["name"]: settings.repo_settings(r).get("allow_auto_merge") for r in s["repo"]}
    assert want.pop("xo-space") is False and set(want.values()) == {True}   # suraj, 2026-10-04
    with pytest.raises(GateError, match="allow_auto_merge must be true or false"):
        settings.repo_settings({"name": "x", "allow_auto_merge": "yes"})


def test_plan_repo_settings_reads_then_plans_a_patch(monkeypatch):
    sent = []
    monkeypatch.setattr(github, "_send", lambda m, url, token, body=None: sent.append((m, url)) or
                        {"allow_auto_merge": False})
    [c] = github.plan_repo_settings("quirq-ai", "gate", {"allow_auto_merge": True}, "t")
    assert sent == [("GET", "https://api.github.com/repos/quirq-ai/gate")]
    assert (c["action"], c["method"], c["body"], c["now"]) == ("update", "PATCH", {"allow_auto_merge": True}, False)
    monkeypatch.setattr(github, "_send", lambda *a, **k: {"allow_auto_merge": True})
    assert github.plan_repo_settings("quirq-ai", "gate", {"allow_auto_merge": True}, "t")[0]["action"] == "unchanged"
    assert github.plan_repo_settings("quirq-ai", "gate", {}, "t") == []


def test_apply_writes_a_setting_without_overwrite(apply_env, config_root, tmp_path, capsys):
    apply_env.setattr(github, "plan_repo", lambda *a: [])
    apply_env.setattr(github, "plan_repo_settings", lambda owner, repo, wanted, token: [
        {"where": f"{owner}/{repo}", "name": "allow_auto_merge", "kind": "setting", "action": "update",
         "method": "PATCH", "url": f"https://api.github.com/repos/{owner}/{repo}", "body": wanted, "diff": [],
         "now": False}])
    sent = []
    apply_env.setattr(github, "_send", lambda m, url, token, body=None: sent.append((m, url, body)) or {})
    assert main(_apply_args(config_root, tmp_path, "sync")) == 0
    out = capsys.readouterr().out
    assert "plan     quirq-ai/sync: update setting allow_auto_merge = true (now false)" in out and sent == []
    assert "(differs: " not in out   # apply.sh asks about 'differs' only for rulesets
    assert main(_apply_args(config_root, tmp_path, "sync", extra=["--yes"])) == 0
    assert sent == [("PATCH", "https://api.github.com/repos/quirq-ai/sync", {"allow_auto_merge": True})]
    assert "quirq-ai/sync: update setting allow_auto_merge = true" in capsys.readouterr().out

import copy

import pytest

from qqgate import settings
from qqgate.backends import github
from qqgate.cli import main
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
    assert plans["xo-space"].checks == ("xo-space-presubmit", "tests")
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
    _, branches, tags = plans_by_name(s, cfg, config_root)["gate"].rulesets
    assert branches["conditions"]["ref_name"]["include"] == ["refs/heads/lkgr", "refs/heads/channels/**"]
    assert tags["target"] == "tag"
    for rs in (branches, tags):
        assert {r["type"] for r in rs["rules"]} == {"creation", "update", "deletion", "non_fast_forward"}
        assert rs["bypass_actors"] == []   # until suraj names the release executor


def test_repo_without_checks_still_goes_through_the_queue(s, cfg, config_root):
    rules = plans_by_name(s, cfg, config_root)["gardener"].rulesets[0]["rules"]
    types = {r["type"] for r in rules}
    assert "merge_queue" in types and "required_status_checks" not in types


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


def test_apply_creates_then_updates_by_name(monkeypatch):
    calls = []

    def fake(method, url, token, body=None):
        calls.append((method, url))
        return [{"name": "qq-main", "id": 7}] if method == "GET" else {}

    monkeypatch.setattr(github, "_send", fake)
    wanted = [{"name": "qq-main"}, {"name": "qq-release-refs-branches"}]
    assert github.apply("quirq-ai", "gate", wanted, "t", write=False)[0].endswith("(dry run)")
    assert [c[0] for c in calls] == ["GET"]
    github.apply("quirq-ai", "gate", wanted, "t", write=True)
    assert calls[-2:] == [("PUT", "https://api.github.com/repos/quirq-ai/gate/rulesets/7"),
                          ("POST", "https://api.github.com/repos/quirq-ai/gate/rulesets")]


def test_apply_without_token_is_a_clear_error(config_root, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("QQ_GITHUB_TOKEN", raising=False)
    _checkout(tmp_path, "sync", "on: [pull_request, merge_group]\njobs:\n  test:\n    runs-on: x\n")
    assert main(["settings", "apply", "--config", str(config_root), "--repo", "sync",
                 "--checkouts", str(tmp_path)]) == 2
    assert "QQ_GITHUB_TOKEN" in capsys.readouterr().err


def test_plan_prints_json(config_root, capsys):
    assert main(["settings", "plan", "--config", str(config_root), "--repo", "gate"]) == 0
    assert '"qq-main"' in capsys.readouterr().out


def test_org_ruleset_runs_drift_in_every_product_repo(s, cfg):
    rs = github.org_ruleset(s, cfg, repository_id=42)
    assert rs["conditions"]["repository_name"]["include"] == ["innernet", "xo-space"]
    wf = rs["rules"][0]["parameters"]["workflows"][0]
    assert wf == {"path": ".github/workflows/qq-drift.yml", "repository_id": 42, "ref": "refs/heads/main"}


@pytest.mark.parametrize("job,why", [
    ("  test:\n    needs: build\n    runs-on: x\n", "without `if: always()`"),
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
                   "  test:\n    needs: b\n    if: always()\n    runs-on: x\n")
    assert settings.readiness(plan, co) == []
    co2 = _checkout(tmp_path, "y", "on: [pull_request, merge_group]\njobs:\n  test:\n    if: false\n")
    assert settings.readiness(plan, co2, allow_conditional=("test",)) == []


def test_path_filtered_and_duplicate_names_are_not_ready(s, cfg, config_root, tmp_path):
    plan = plans_by_name(s, cfg, config_root)["sync"]
    co = _checkout(tmp_path, "x", "on:\n  pull_request:\n    paths: [src/**]\n  merge_group:\njobs:\n  test:\n    runs-on: x\n")
    (co / ".github" / "workflows" / "other.yml").write_text("on: [pull_request, merge_group]\njobs:\n  test:\n    runs-on: x\n")
    whys = settings.readiness(plan, co)
    assert any("paths" in w for w in whys) and any("several workflows" in w for w in whys)


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

import shutil

from qqgate import backends, required
from qqgate.backends import github


def test_generated_workflows_produce_every_required_check(cfg, config_root):
    for repo in ("xo-space", "innernet"):
        assert github.check_workflows(config_root, required.compute(cfg, repo)) == []


def test_missing_merge_group_trigger_is_caught(cfg, config_root, tmp_path):
    shutil.copytree(config_root / "generated", tmp_path / "generated")
    wf = tmp_path / "generated" / "github" / "xo-space" / "qq-xo-space-presubmit.yml"
    wf.write_text(wf.read_text().replace("  merge_group:\n", ""))
    problems = github.check_workflows(tmp_path, required.compute(cfg, "xo-space"))
    assert any("merge_group" in p for p in problems)


def test_renamed_job_is_caught(cfg, config_root, tmp_path):
    shutil.copytree(config_root / "generated", tmp_path / "generated")
    wf = tmp_path / "generated" / "github" / "innernet" / "qq-innernet-presubmit.yml"
    wf.write_text(wf.read_text().replace("  innernet-presubmit:\n", "  presubmit:\n"))
    problems = github.check_workflows(tmp_path, required.compute(cfg, "innernet"))
    assert any("no job 'innernet-presubmit'" in p for p in problems)


def test_rule_pins_checks_to_github_actions(cfg):
    rule = github.required_checks_rule(required.compute(cfg, "xo-space"))
    assert rule["type"] == "required_status_checks"
    assert rule["parameters"]["required_status_checks"] == [
        {"context": "xo-space-presubmit", "integration_id": github.GITHUB_ACTIONS_APP_ID}]


def test_observe_takes_newest_run(monkeypatch):
    runs = {"total_count": 2, "check_runs": [
        {"name": "xo-space-presubmit", "conclusion": "failure", "status": "completed", "started_at": "2026-10-04T10:00:00Z"},
        {"name": "xo-space-presubmit", "conclusion": "success", "status": "completed", "started_at": "2026-10-04T11:00:00Z"},
    ]}
    monkeypatch.setattr(github, "_get", lambda url, token: runs)
    assert github.observe("github.com/quirq-ai/xo-space", "abc", token="") == {"xo-space-presubmit": "success"}


def test_backend_loader():
    assert backends.load("github") is github

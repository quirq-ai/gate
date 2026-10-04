import json

from qqgate import required, verdict
from qqgate.cli import main
from tests.conftest import FIXTURES


def test_red_check_is_refused(cfg):
    v = verdict.evaluate(required.compute(cfg, "xo-space"), {"xo-space-presubmit": "failure"})
    assert not v.passed and v.failing == (("xo-space-presubmit", "failure"),)


def test_missing_check_is_refused_not_passed(cfg):
    v = verdict.evaluate(required.compute(cfg, "innernet"), {"some-other-check": "success"})
    assert not v.passed and v.missing == ("innernet-presubmit",)


def test_running_and_skipped_are_not_green(cfg):
    req = required.compute(cfg, "xo-space")
    for state in ("in_progress", "queued", "skipped", "neutral", "cancelled", "timed_out"):
        assert not verdict.evaluate(req, {"xo-space-presubmit": state}).passed


def test_green_passes(cfg):
    assert verdict.evaluate(required.compute(cfg, "xo-space"), {"xo-space-presubmit": "success"}).passed


def test_cli_refuses_red_pr_in_both_repos(config_root, capsys):
    for repo in ("xo-space", "innernet"):
        assert main(["verdict", "--config", str(config_root), "--repo", repo,
                     "--observed", str(FIXTURES / "red.json")]) == 1
        assert "REFUSED" in capsys.readouterr().out


def test_cli_passes_green(config_root, capsys):
    assert main(["verdict", "--config", str(config_root), "--repo", "xo-space",
                 "--observed", str(FIXTURES / "green.json"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"

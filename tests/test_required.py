import pytest

from qqgate import required
from qqgate.errors import GateError
from tests.conftest import FIXTURES


def test_xo_space_required_is_its_blocking_presubmit(cfg):
    req = required.compute(cfg, "xo-space")
    assert req.names == ["xo-space-presubmit"]
    assert req.backend == "github" and req.merge_method == "squash"
    assert set(req.checks[0].triggers) >= {"change", "queue"}


def test_innernet_required_is_its_blocking_presubmit(cfg):
    assert required.compute(cfg, "innernet").names == ["innernet-presubmit"]


def test_postsubmit_and_release_builders_are_not_required(cfg):
    names = required.compute(cfg, "xo-space").names
    assert "xo-space-postsubmit" not in names and "xo-space-canary-deploy" not in names


def test_blocking_builder_must_also_run_in_the_merge_queue(cfg):
    b = next(b for b in cfg["pipelines"]["builder"] if b["name"] == "xo-space-presubmit")
    b["triggers"] = ["change"]
    with pytest.raises(GateError, match="exact merge result"):
        required.compute(cfg, "xo-space")


def test_repo_with_no_blocking_builder_is_ungated_not_empty(cfg):
    for b in cfg["pipelines"]["builder"]:
        if b["repo"] == "innernet":
            b["blocking"] = False
    with pytest.raises(GateError, match="nothing would gate it"):
        required.compute(cfg, "innernet")


def test_unknown_repo_is_refused(cfg):
    with pytest.raises(GateError, match="not an onboarded repo"):
        required.compute(cfg, "no-such-repo")


def test_unknown_rule_is_refused(cfg):
    cfg["gate"]["merge_queue"]["required"] = "everything"
    with pytest.raises(GateError, match="not a rule"):
        required.compute(cfg, "xo-space")


@pytest.mark.parametrize("repo", ["xo-space", "innernet"])
def test_manifest_targets_are_covered(cfg, repo):
    manifest = required.load_manifest(FIXTURES / f"{repo}.repo.toml", cfg)
    req = required.compute(cfg, repo, manifest)
    assert req.manifest_targets


def test_manifest_target_without_a_blocking_builder_is_refused(cfg):
    manifest = required.load_manifest(FIXTURES / "xo-space.repo.toml", cfg)
    manifest["targets"].append({"name": "site", "kind": "static-docs"})
    with pytest.raises(GateError, match=r"site \(static-docs\)"):
        required.compute(cfg, "xo-space", manifest)

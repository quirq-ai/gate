import json
import pytest

from qqgate.cli import main


def test_no_command_is_a_usage_error():
    with pytest.raises(SystemExit) as e:
        main([])
    assert e.value.code == 2


def test_bad_config_path_is_a_clear_error(tmp_path, capsys):
    assert main(["required", "--config", str(tmp_path), "--repo", "xo-space"]) == 2
    assert "not an infra-config checkout" in capsys.readouterr().err


def test_bad_observed_file_cannot_look_like_a_refusal(config_root, tmp_path, capsys):
    from tests.conftest import infra_config_root  # noqa: F401  (config_root fixture needs it)
    for content in (None, "[1, 2]", '{"xo-space-presubmit": 1}', "not json"):
        f = tmp_path / "o.json"
        if content is None:
            f = tmp_path / "missing.json"
        else:
            f.write_text(content)
        assert main(["verdict", "--config", str(config_root), "--repo", "xo-space", "--observed", str(f)]) == 2


def test_crash_is_could_not_decide(monkeypatch, config_root):
    from qqgate import verdict

    def boom(*a):
        raise RuntimeError("bug")
    monkeypatch.setattr(verdict, "evaluate", boom)
    from tests.conftest import FIXTURES
    assert main(["verdict", "--config", str(config_root), "--repo", "xo-space",
                 "--observed", str(FIXTURES / "red.json")]) == 2


def test_not_onboarded_has_its_own_exit_code(config_root, capsys):
    for cmd in ("required", "rule"):
        assert main([cmd, "--config", str(config_root), "--repo", "no-such-repo"]) == 3
        assert "is not an onboarded repo" in capsys.readouterr().err
    assert main(["required", "--config", str(config_root), "--repo", "no-such-repo", "--json"]) == 3
    assert json.loads(capsys.readouterr().out) == {"repo": "no-such-repo", "onboarded": False}

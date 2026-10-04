import pytest

from qqgate.cli import main


def test_no_command_is_a_usage_error():
    with pytest.raises(SystemExit) as e:
        main([])
    assert e.value.code == 2


def test_bad_config_path_is_a_clear_error(tmp_path, capsys):
    assert main(["required", "--config", str(tmp_path), "--repo", "xo-space"]) == 2
    assert "not an infra-config checkout" in capsys.readouterr().err

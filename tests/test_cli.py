from qqgate.cli import main


def test_help_runs():
    assert main([]) == 0

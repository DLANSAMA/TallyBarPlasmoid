"""The suite never points backend state at the developer's real home, during a test or
between two: a worker thread a test leaves running keeps saving after the test's patches
are undone, to wherever the path constants then point."""
from pathlib import Path

import pytest

import conftest


def _constants_under(home: Path) -> list[str]:
    found = []
    for module in conftest.code_modules():
        for name, value in vars(module).items():
            for path in value if isinstance(value, tuple) else (value,):
                if (isinstance(path, Path) and path.is_relative_to(home)
                        and not path.is_relative_to(conftest._REPO)):
                    found.append(f"{module.__name__}.{name}")
    return found


def test_state_paths_point_into_the_test_home(tmp_path):
    import backend  # noqa: F401
    import providers.cost as cost

    assert cost.ANTIGRAVITY_LEDGER_PATH.is_relative_to(tmp_path)
    assert cost.COST_ARCHIVE_PATH.is_relative_to(tmp_path)
    assert Path.home().is_relative_to(tmp_path)
    assert _constants_under(conftest._REAL_HOME) == []


class TestBetweenTests:
    @pytest.fixture(scope="class")
    def resting(self):
        # Class scope is set up before the per-test isolation fixture, so this sees what
        # the previous test's teardown left behind.
        import backend  # noqa: F401
        import providers.cost as cost

        return {
            "home": Path.home(),
            "ledger": cost.ANTIGRAVITY_LEDGER_PATH,
            "under_real_home": _constants_under(conftest._REAL_HOME),
        }

    def test_state_paths_rest_off_the_real_home(self, resting):
        assert resting["home"] == conftest._SESSION_HOME
        assert not resting["ledger"].is_relative_to(conftest._REAL_HOME)
        assert resting["under_real_home"] == []

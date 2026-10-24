"""The Nix settings of the pytest process, and a guard that keeps them still.

A setting is a process global: `huggorm.set_setting` writes Nix's
`globalConfig`, and every test after the writer reads what it left. The guard
names the test that left a change, instead of a later test failing for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import huggorm
from huggorm.testing.environment import EXPERIMENTAL_FEATURES

if TYPE_CHECKING:
    from collections.abc import Iterator

#: What every store here needs, set before the first store opens. A store
#: reads the globals once, and enabling `ca-derivations` after a local store
#: opened without it aborts the process.
BASE_SETTINGS = {
    "build-users-group": "",
    "require-drop-supplementary-groups": "false",
}

_settled: dict[str, str] | None = None
_drift_reported = False


def _changes(old: dict[str, str], new: dict[str, str]) -> list[str]:
    return sorted(
        f"{name}: {old.get(name)!r} -> {value!r}"
        for name, value in new.items()
        if old.get(name) != value
    )


@pytest.fixture(scope="session", autouse=True)
def nix_base_settings() -> None:
    for name, value in BASE_SETTINGS.items():
        huggorm.set_setting(name, value)
    for feature in EXPERIMENTAL_FEATURES:
        huggorm.enable_experimental_feature(feature)


@pytest.fixture(autouse=True)
def no_process_wide_nix_setting_survives_a_test(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail the test that leaves a Nix setting changed in this process.

    The first test is exempt: the first session of a process applies its own
    settings (`nix-path` from `NIX_PATH`, for one), and those stay for the run.
    A `forked` test is exempt, because its writes end with its child.
    A change made by a wider-scoped fixture shows at the setup of the next
    test, once, until the settings come back.
    """
    global _settled, _drift_reported
    if "forked" in request.keywords:
        yield
        return
    before = huggorm.list_settings()
    drift = _changes(_settled, before) if _settled is not None else []
    if not drift:
        _drift_reported = False
    elif not _drift_reported:
        _drift_reported = True
        pytest.fail(
            "a Nix setting moved between the last test and this one. A fixture set up "
            "or torn down in between moved it; this test asked for "
            + ", ".join(request.fixturenames)
            + "\n  "
            + "\n  ".join(drift),
            pytrace=False,
        )
    yield
    after = huggorm.list_settings()
    if _settled is None:
        _settled = after
        return
    changed = _changes(
        before, {name: value for name, value in after.items() if _settled.get(name) != value}
    )
    if changed:
        pytest.fail(
            "this test left a Nix setting changed in the pytest process, where every "
            "test after it reads the value. Put the setting back, or mark the test "
            "`@pytest.mark.forked`.\n  " + "\n  ".join(changed),
            pytrace=False,
        )

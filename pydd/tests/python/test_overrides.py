"""Node overrides, from the inputs of a planner to the node that builds a package."""

from __future__ import annotations

import pytest

from pydd.python import Artifact, Plan, overrides_from


def sdist(package: str) -> Artifact:
    return Artifact(package=package, version="1.0", filename=f"{package}-1.0.tar.gz", kind="sdist")


def test_the_name_is_canonical_and_config_settings_are_json() -> None:
    given = {"overrides": {"NumPy": {"configSettings": {"setup-args": ["-Db=1"], "a": "x"}, "fixup": False}}}
    assert overrides_from(given) == {"numpy": {"configSettings": '{"a":"x","setup-args":["-Db=1"]}', "fixup": False}}


@pytest.mark.parametrize(
    "override",
    [{"preBuld": "true"}, {"fixup": "no"}, {"env": {"N": 1}}],
)
def test_a_wrong_key_or_type_is_refused(override: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="override of 'pkg'"):
        overrides_from({"overrides": {"pkg": override}})


def test_only_the_named_package_gets_the_override() -> None:
    plan = Plan(overrides=overrides_from({"overrides": {"numpy": {"preBuild": "true"}}}))
    plan.artifact_node(sdist("numpy"), "sdist-build", backend="mesonpy")
    plan.artifact_node(sdist("idna"), "sdist-build", backend="flit_core.buildapi")
    assert plan.nodes[0]["overrides"] == {"preBuild": "true"}
    assert "overrides" not in plan.nodes[1]

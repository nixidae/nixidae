"""Probes: how an answer is judged, how an annotation parses, how long a wait lasts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from kappy.readiness import (
    PROBE_ANNOTATION,
    Denied,
    Matches,
    NotEffectiveError,
    Probe,
    UnusableProbeError,
    check,
    probe_of,
    project,
    wait_effective,
)

if TYPE_CHECKING:
    from kappy.apply import JsonValue, Manifest


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


POD: Manifest = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {"namespace": "images", "name": "kappy-probe"},
    "spec": {"containers": [{"name": "c", "image": "busybox"}]},
}
REWRITTEN: JsonValue = {"spec": {"containers": [{"image": "127.0.0.1:5000/busybox"}]}}


def annotated(value: JsonValue) -> Manifest:
    return {"kind": "MutatingAdmissionPolicyBinding", "metadata": {"annotations": {PROBE_ANNOTATION: value}}}


class TestProject:
    def test_keeps_only_the_named_keys(self) -> None:
        actual: JsonValue = {"a": 1, "b": {"c": 2, "d": 3}}
        expected: JsonValue = {"b": {"c": 2}}
        assert project(actual, {"b": {"c": 0}}) == expected

    def test_drops_a_missing_key_so_containment_fails(self) -> None:
        assert project({"a": 1}, {"b": 1}) == {}

    def test_pairs_lists_by_position_and_keeps_the_extra(self) -> None:
        actual: JsonValue = [{"image": "x", "name": "a"}, {"image": "y", "name": "b"}]
        expected: JsonValue = [{"image": "x"}, {"image": "y", "name": "b"}]
        assert project(actual, [{"image": ""}]) == expected

    def test_a_scalar_is_itself(self) -> None:
        assert project("x", {"a": 1}) == "x"


class TestMatches:
    def test_contained_is_effective(self) -> None:
        response: Manifest = {"spec": {"containers": [{"name": "c", "image": "127.0.0.1:5000/busybox"}]}}
        assert Matches(REWRITTEN).admitted(response) is None

    def test_unmutated_names_both_sides(self) -> None:
        verdict = Matches(REWRITTEN).admitted(POD)
        assert verdict is not None
        assert "127.0.0.1:5000/busybox" in verdict
        assert '"image":"busybox"' in verdict

    def test_a_list_of_another_length_does_not_match(self) -> None:
        response: Manifest = {
            "spec": {"containers": [{"image": "127.0.0.1:5000/busybox"}, {"image": "other"}]},
        }
        assert Matches(REWRITTEN).admitted(response) is not None

    def test_a_refusal_is_not_effective(self) -> None:
        verdict = Matches(REWRITTEN).refused("pods is forbidden: violates PodSecurity")
        assert verdict is not None
        assert "PodSecurity" in verdict


class TestDenied:
    def test_refused_with_the_substring_is_effective(self) -> None:
        assert Denied("solid-trust").refused("ValidatingAdmissionPolicy 'solid-trust' denied request") is None

    def test_refused_for_another_reason_is_not(self) -> None:
        verdict = Denied("solid-trust").refused("violates PodSecurity")
        assert verdict is not None
        assert "PodSecurity" in verdict

    def test_admitted_is_not(self) -> None:
        assert Denied("solid-trust").admitted(POD) is not None


class TestProbeOf:
    def test_no_annotation_is_no_probe(self) -> None:
        assert probe_of({"kind": "Pod", "metadata": {"name": "x"}}) is None

    def test_matches(self) -> None:
        probe = probe_of(annotated(json.dumps({"object": POD, "matches": REWRITTEN})))
        assert probe == Probe(POD, Matches(REWRITTEN))

    def test_denied(self) -> None:
        probe = probe_of(annotated(json.dumps({"object": POD, "denied": "solid-trust"})))
        assert probe == Probe(POD, Denied("solid-trust"))

    @pytest.mark.parametrize(
        ("value", "complaint"),
        [
            (1, "not a string"),
            ("{", "not JSON"),
            ("[]", "not a JSON object"),
            (json.dumps({"matches": {}}), "`object`"),
            (json.dumps({"object": {"metadata": {}}, "matches": {}}), "`object`"),
            (json.dumps({"object": POD}), "neither"),
            (json.dumps({"object": POD, "matches": {}, "denied": "x"}), "exactly one"),
            (json.dumps({"object": POD, "denied": ""}), "non-empty"),
        ],
    )
    def test_malformed_names_the_problem(self, value: JsonValue, complaint: str) -> None:
        with pytest.raises(ValueError, match=complaint):
            probe_of(annotated(value))


class TestCheck:
    @pytest.mark.anyio
    async def test_a_refusal_is_judged_not_raised(self) -> None:
        async def refuse(_request: Manifest) -> Manifest:
            msg = "admission webhook denied: solid-trust"
            raise RuntimeError(msg)

        assert await check(Probe(POD, Denied("solid-trust")), refuse) is None
        assert await check(Probe(POD, Matches(REWRITTEN)), refuse) is not None

    @pytest.mark.anyio
    async def test_an_unusable_probe_raises(self) -> None:
        async def exists(_request: Manifest) -> Manifest:
            msg = "exists"
            raise UnusableProbeError(msg)

        with pytest.raises(UnusableProbeError):
            await check(Probe(POD, Matches(REWRITTEN)), exists)


class TestWaitEffective:
    @pytest.mark.anyio
    async def test_returns_once_the_answer_shows_it(self) -> None:
        answers: list[Manifest] = [POD, POD, {"spec": {"containers": [{"image": "127.0.0.1:5000/busybox"}]}}]
        slept: list[float] = []

        async def dry_run(_request: Manifest) -> Manifest:
            return answers.pop(0)

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        await wait_effective(
            Probe(POD, Matches(REWRITTEN)),
            dry_run,
            what="binding",
            seconds=60,
            clock=lambda: 0.0,
            sleep=sleep,
        )
        assert answers == []
        assert slept == [0.5, 1.0]

    @pytest.mark.anyio
    async def test_names_the_object_and_the_last_answer_at_the_deadline(self) -> None:
        now = [0.0]

        async def dry_run(_request: Manifest) -> Manifest:
            return POD

        async def sleep(seconds: float) -> None:
            now[0] += seconds

        with pytest.raises(NotEffectiveError, match=r"MutatingAdmissionPolicyBinding/x .* within 5s.*busybox"):
            await wait_effective(
                Probe(POD, Matches(REWRITTEN)),
                dry_run,
                what="MutatingAdmissionPolicyBinding/x",
                seconds=5,
                clock=lambda: now[0],
                sleep=sleep,
            )

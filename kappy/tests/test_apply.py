from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

import anyio
import pytest
from conventions import apply_and_prune, converge_direct, field_manager_for, prune_selector

from kappy.apply import (
    JsonValue,
    Manifest,
    Resource,
    _wait_established,
    discover,
)
from kappy.testing import FakeApi

if TYPE_CHECKING:
    from kr8s.asyncio.objects import APIObject


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class TestDiscover:
    """Resolving a custom kind against the API server's discovery document.

    The whole reason `kappy.apply.discover` exists rather than calling kr8s'
    `Api.async_lookup_kind`: that lowercases the Kind and then compares it to a
    resource's plural, Kind, singular and short names, so a CRD whose singular
    is not the lowercased Kind matches nothing at all.
    """

    # The multus CRD, which is where this was found. Its singular carries
    # hyphens, so the lowercased Kind -- "networkattachmentdefinition" --
    # equals neither the plural, nor the Kind, nor the singular.
    MULTUS: ClassVar[Resource] = {
        "version": "k8s.cni.cncf.io/v1",
        "kind": "NetworkAttachmentDefinition",
        "name": "network-attachment-definitions",
        "singularName": "network-attachment-definition",
        "namespaced": True,
    }

    # KubeVirt's, for contrast. Its singular *is* the lowercased Kind, so it
    # resolved under the old lookup and still has to resolve under this one.
    # The pair is the point: the fix identifies a kind exactly, rather than
    # widening a match until the broken case slips through.
    KUBEVIRT: ClassVar[Resource] = {
        "version": "kubevirt.io/v1",
        "kind": "VirtualMachineInstance",
        "name": "virtualmachineinstances",
        "singularName": "virtualmachineinstance",
        "namespaced": True,
    }

    async def test_resolves_a_kind_whose_singular_is_not_the_lowercased_kind(self) -> None:
        api = FakeApi(resources=[self.MULTUS, self.KUBEVIRT])

        plural, namespaced = await discover(api, "NetworkAttachmentDefinition", "k8s.cni.cncf.io/v1")  # type: ignore[arg-type]

        assert plural == "network-attachment-definitions"
        assert namespaced is True

    async def test_resolves_a_kind_whose_singular_is_the_lowercased_kind(self) -> None:
        api = FakeApi(resources=[self.MULTUS, self.KUBEVIRT])

        plural, namespaced = await discover(api, "VirtualMachineInstance", "kubevirt.io/v1")  # type: ignore[arg-type]

        assert plural == "virtualmachineinstances"
        assert namespaced is True

    async def test_matches_on_the_group_version_too(self) -> None:
        """Two groups can serve the same Kind. The manifest names both fields,
        so both have to agree."""
        api = FakeApi(resources=[self.MULTUS])

        with pytest.raises(ValueError, match=r"k8s\.cni\.cncf\.io/v1alpha1"):
            await discover(api, "NetworkAttachmentDefinition", "k8s.cni.cncf.io/v1alpha1")  # type: ignore[arg-type]

    async def test_reads_discovery_again_when_the_cache_does_not_have_the_kind(self) -> None:
        """A CRD an earlier barrier created is not in a cache filled before it
        existed, so a miss has to reach the API server before giving up."""
        api = FakeApi(resources=[self.MULTUS])

        await discover(api, "NetworkAttachmentDefinition", "k8s.cni.cncf.io/v1")  # type: ignore[arg-type]
        assert api.uncached_reads == 0

        with pytest.raises(ValueError, match="Missing"):
            await discover(api, "Missing", "example.com/v1")  # type: ignore[arg-type]
        assert api.uncached_reads == 1

    async def test_applies_a_custom_resource_of_a_hyphenated_kind(self) -> None:
        """End to end through `apply_and_prune`, which is where it failed:
        the apply died on the CR seconds after waiting for its own CRD."""
        spec: Manifest = {
            "apiVersion": "k8s.cni.cncf.io/v1",
            "kind": "NetworkAttachmentDefinition",
            "metadata": {"name": "dynhetz", "namespace": "kube-system"},
        }
        api = FakeApi(resources=[self.MULTUS])

        await apply_and_prune([spec], api=api.as_api(), environment="full", prune=False)

        assert api.patched == [("kube-system", "NetworkAttachmentDefinition", "dynhetz")]


class TestApplyAndPrune:
    async def test_does_not_prune_object_it_just_applied(self) -> None:
        """Regression test for the exact bug this fixes: kr8s reports a
        just-applied CRD object's kind in a different case when listing it
        back for the prune scan -- that must not cause it to be pruned."""
        spec: Manifest = {
            "apiVersion": "autoscaling.k8s.io/v1",
            "kind": "VerticalPodAutoscaler",
            "metadata": {"name": "argocd-server", "namespace": "argocd"},
        }
        api = FakeApi(listed=[("argocd", "verticalpodautoscaler", "argocd-server")])

        await apply_and_prune([spec], api=api.as_api(), environment="full")

        assert api.deleted == []

    async def test_prunes_genuinely_stale_object(self) -> None:
        spec: Manifest = {
            "apiVersion": "autoscaling.k8s.io/v1",
            "kind": "VerticalPodAutoscaler",
            "metadata": {"name": "argocd-server", "namespace": "argocd"},
        }
        api = FakeApi(
            listed=[
                ("argocd", "verticalpodautoscaler", "argocd-server"),
                ("argocd", "verticalpodautoscaler", "long-gone"),
            ]
        )

        await apply_and_prune([spec], api=api.as_api(), environment="full")

        assert api.deleted == [("argocd", "verticalpodautoscaler", "long-gone")]


class TestPruneLeavesOwnedObjects:
    """An object with `ownerReferences` is never pruned.

    Measured on nixlab2 (2026-09-17): all eight External Secrets Operator
    Secrets carry `ownerReferences: [ExternalSecret]` and hold the only copy
    of every Harbor and oauth2-proxy credential. They stay outside the
    environment-labelled scope today only because ESO does not copy
    `ekn.dev/environment` into what it materialises -- someone else's
    template. This guard does not depend on that one holding.
    """

    SPEC: ClassVar[Manifest] = {
        "apiVersion": "autoscaling.k8s.io/v1",
        "kind": "VerticalPodAutoscaler",
        "metadata": {"name": "argocd-server", "namespace": "argocd"},
    }

    async def test_an_owned_object_survives_a_prune_that_would_take_it(self) -> None:
        api = FakeApi(
            listed=[
                ("argocd", "verticalpodautoscaler", "argocd-server"),
                ("argocd", "verticalpodautoscaler", "materialised"),
            ],
            extra_metadata={
                "materialised": {"ownerReferences": [{"kind": "ExternalSecret", "name": "harbor-admin"}]},
            },
        )

        await apply_and_prune([self.SPEC], api=api.as_api(), environment="full")

        assert api.deleted == []

    async def test_an_empty_owner_reference_list_is_not_an_owner(self) -> None:
        """The API server drops the key when the last owner goes, but a
        client that writes `[]` must not read as owned -- that would make the
        guard silently unprunable-by-default."""
        api = FakeApi(
            listed=[
                ("argocd", "verticalpodautoscaler", "argocd-server"),
                ("argocd", "verticalpodautoscaler", "long-gone"),
            ],
            extra_metadata={"long-gone": {"ownerReferences": []}},
        )

        await apply_and_prune([self.SPEC], api=api.as_api(), environment="full")

        assert api.deleted == [("argocd", "verticalpodautoscaler", "long-gone")]


class TestPruneLeavesObjectsItNeverApplied:
    """An object no delivery manager has touched is never pruned.

    Measured on nixlab2 (2026-09-17): of 111 objects inside this selector,
    14 would have been deleted by a prune that checked labels alone, and
    none had ever been applied by `ekn` or the engine. They are
    controller-generated children that inherit their parent's labels -- the
    endpoints controller copies a Service's onto its Endpoints and
    EndpointSlice. Six of those carry no `ownerReferences` at all, and one
    of the six is `kube-system/coredns`.
    """

    SPEC: ClassVar[Manifest] = {
        "apiVersion": "autoscaling.k8s.io/v1",
        "kind": "VerticalPodAutoscaler",
        "metadata": {"name": "argocd-server", "namespace": "argocd"},
    }

    async def test_a_controller_generated_child_is_not_pruned(self) -> None:
        api = FakeApi(
            listed=[
                ("argocd", "verticalpodautoscaler", "argocd-server"),
                ("argocd", "verticalpodautoscaler", "inherited"),
            ],
            extra_metadata={
                # No ownerReferences, exactly like a legacy Endpoints object.
                # `_owner` cannot save this one; only the manager check can.
                "inherited": {"managedFields": [{"manager": "kube-controller-manager"}]},
            },
        )

        await apply_and_prune([self.SPEC], api=api.as_api(), environment="full")

        assert api.deleted == []

    async def test_an_object_we_applied_is_still_pruned(self) -> None:
        """The control, and without it the guards could be made to pass by
        deleting nothing at all. An object `ekn` applied, carrying both
        labels and absent from this generation, is what a prune is for."""
        api = FakeApi(
            listed=[
                ("argocd", "verticalpodautoscaler", "argocd-server"),
                ("argocd", "verticalpodautoscaler", "removed-from-config"),
            ],
            extra_metadata={
                "removed-from-config": {"managedFields": [{"manager": "ekn"}]},
            },
        )

        await apply_and_prune([self.SPEC], api=api.as_api(), environment="full")

        assert api.deleted == [("argocd", "verticalpodautoscaler", "removed-from-config")]

    async def test_the_units_own_field_manager_counts_as_delivery(self) -> None:
        """A unit applies as the controller that takes its objects over, so a
        prune that only knew `ekn` would refuse to delete anything that unit
        had ever applied."""
        spec: Manifest = {
            "apiVersion": "autoscaling.k8s.io/v1",
            "kind": "VerticalPodAutoscaler",
            "metadata": {
                "name": "argocd-server",
                "namespace": "argocd",
                "labels": {"ekn.dev/deployment-unit": "bootstrap"},
            },
        }
        api = FakeApi(
            listed=[
                ("argocd", "verticalpodautoscaler", "argocd-server"),
                ("argocd", "verticalpodautoscaler", "gone"),
            ],
            extra_metadata={"gone": {"managedFields": [{"manager": "argocd-controller"}]}},
        )

        await apply_and_prune(
            [spec],
            api=api.as_api(),
            environment="full",
            unit="bootstrap",
            field_manager="argocd-controller",
        )

        assert api.deleted == [("argocd", "verticalpodautoscaler", "gone")]


class TestPruneWarnsAboutUndeclaredUnits:
    """Deleting an object of a unit the configuration no longer declares is
    correct, and is also one line of config away from deleting ArgoCD and the
    CNI. Before the set-based selector the not-exists clause hid the case.
    """

    SPEC: ClassVar[Manifest] = {
        "apiVersion": "autoscaling.k8s.io/v1",
        "kind": "VerticalPodAutoscaler",
        "metadata": {"name": "argocd-server", "namespace": "argocd"},
    }

    async def test_it_still_deletes_and_it_says_which_unit(self, capsys: pytest.CaptureFixture[str]) -> None:
        api = FakeApi(
            listed=[
                ("argocd", "verticalpodautoscaler", "argocd-server"),
                ("argocd", "verticalpodautoscaler", "orphan"),
            ],
            extra_metadata={"orphan": {"labels": {"ekn.dev/deployment-unit": "retired"}}},
        )

        await apply_and_prune(
            [self.SPEC],
            api=api.as_api(),
            environment="full",
            declared_units=["bootstrap"],
        )

        assert api.deleted == [("argocd", "verticalpodautoscaler", "orphan")]
        # structlog writes to stdout rather than through `logging`, so
        # `caplog` stays empty here however loudly the warning fires.
        assert "retired" in capsys.readouterr().out

    async def test_a_declared_unit_is_pruned_without_the_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        api = FakeApi(
            listed=[
                ("argocd", "verticalpodautoscaler", "argocd-server"),
                ("argocd", "verticalpodautoscaler", "orphan"),
            ],
            extra_metadata={"orphan": {"labels": {"ekn.dev/deployment-unit": "routed"}}},
        )

        await apply_and_prune(
            [self.SPEC],
            api=api.as_api(),
            environment="full",
            declared_units=["routed"],
        )

        assert api.deleted == [("argocd", "verticalpodautoscaler", "orphan")]
        assert "no longer declares" not in capsys.readouterr().out


class TestFieldManagerPerUnit:
    """Which manager a whole-instance apply writes each object as.

    Measured on nixlab2: all 37 Kubernetes units declare `fieldManager =
    "argocd-controller"`, and 919/919 generated objects carry a unit label
    -- but a whole-instance apply is one group, so it wrote everything as
    `ekn`. That is right for an apply that runs again and wrong for a full
    deploy standing in for a paused engine.
    """

    UNITS: ClassVar[dict[str, str]] = {"vm-logs": "argocd-controller", "plain": "ekn"}

    @staticmethod
    def _spec(unit: str | None) -> Manifest:
        labels: dict[str, JsonValue] = {"ekn.dev/deployment-unit": unit} if unit else {}
        return {"kind": "ConfigMap", "metadata": {"name": "a", "labels": labels}}

    def test_an_object_applies_as_its_own_units_manager(self) -> None:
        assert field_manager_for(self._spec("vm-logs"), default="ekn", unit_managers=self.UNITS) == "argocd-controller"

    def test_no_unit_managers_means_the_default_for_everything(self) -> None:
        """The gate. `None` is what the caller passes unless the engine is
        actually paused -- two writers sharing one manager name are one
        manager to the API server, so each apply's field set replaces the
        other's, and that is active flapping rather than a slow leak."""
        assert field_manager_for(self._spec("vm-logs"), default="ekn", unit_managers=None) == "ekn"
        assert field_manager_for(self._spec("vm-logs"), default="ekn", unit_managers={}) == "ekn"

    def test_an_unlabelled_object_keeps_the_default(self) -> None:
        assert field_manager_for(self._spec(None), default="ekn", unit_managers=self.UNITS) == "ekn"

    def test_a_unit_the_map_does_not_name_keeps_the_default(self) -> None:
        """A label naming a unit this instance does not declare is left
        alone rather than guessed at -- the same rule `stampRouted` uses."""
        assert field_manager_for(self._spec("gone"), default="ekn", unit_managers=self.UNITS) == "ekn"

    async def test_the_converging_apply_uses_it_per_object(self) -> None:
        """End to end: two objects in one apply, two different managers."""
        api = FakeApi()
        specs: list[Manifest] = [
            {
                "apiVersion": "autoscaling.k8s.io/v1",
                "kind": "VerticalPodAutoscaler",
                "metadata": {"name": "a", "namespace": "argocd", "labels": {"ekn.dev/deployment-unit": "vm-logs"}},
            },
            {
                "apiVersion": "autoscaling.k8s.io/v1",
                "kind": "VerticalPodAutoscaler",
                "metadata": {"name": "b", "namespace": "argocd", "labels": {"ekn.dev/deployment-unit": "plain"}},
            },
        ]

        report, _desired = await converge_direct(
            specs,
            api=api.as_api(),
            environment="full",
            unit_managers=self.UNITS,
        )

        assert report.ok
        assert api.managers == {"a": "argocd-controller", "b": "ekn"}


class TestPruneSelector:
    """The two prune scopes, as the string `kr8s` is handed.

    A raw string is passed through verbatim as `labelSelector`, which is what
    makes the set-based form work at all -- there is no dict spelling of it.
    """

    def test_a_whole_instance_apply_excludes_the_hand_applied_units(self) -> None:
        assert (
            prune_selector(environment="prod", unit=None, hand_applied=["bootstrap", "cni"])
            == "ekn.dev/environment=prod,ekn.dev/deployment-unit notin (bootstrap,cni)"
        )

    def test_the_excluded_units_are_sorted_so_the_selector_is_stable(self) -> None:
        """Nix hands back an attrset, and a selector that reorders between two
        evaluations of the same configuration is a diff nobody can read."""
        assert prune_selector(environment="prod", unit=None, hand_applied={"cni", "bootstrap"}) == prune_selector(
            environment="prod",
            unit=None,
            hand_applied={"bootstrap", "cni"},
        )

    def test_no_hand_applied_units_emits_no_unit_clause(self) -> None:
        """Not `notin ()`. The API server refuses an empty value set --
        `labels.NewRequirement` rejects `in`/`notin` with one -- and a config
        with no nested units is the ordinary shape `ekn validate` applies."""
        assert prune_selector(environment="prod", unit=None) == "ekn.dev/environment=prod"

    def test_a_target_apply_owns_that_unit_and_ignores_the_exclusions(self) -> None:
        assert (
            prune_selector(environment="prod", unit="bootstrap", hand_applied=["bootstrap", "cni"])
            == "ekn.dev/environment=prod,ekn.dev/deployment-unit=bootstrap"
        )


class TestUnitLabelGuard:
    """A `--target` apply must not carry an object outside its own scope.

    The dangerous half is a *missing* label. The object is applied with the
    environment label, this apply's prune never looks at it, and the next
    whole-instance prune takes it as its own and deletes it -- `notin`
    matches an object carrying no unit label at all.
    """

    async def test_an_unlabelled_object_is_refused(self) -> None:
        spec: Manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "raw", "namespace": "argocd"},
        }
        api = FakeApi()

        with pytest.raises(ValueError, match=r"ConfigMap/raw: None"):
            await apply_and_prune([spec], api=api.as_api(), environment="full", unit="bootstrap")

        assert api.patched == []

    async def test_an_object_of_another_unit_is_refused(self) -> None:
        spec: Manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "stray",
                "namespace": "argocd",
                "labels": {"ekn.dev/deployment-unit": "apps"},
            },
        }
        api = FakeApi()

        with pytest.raises(ValueError, match=r"ConfigMap/stray: 'apps'"):
            await apply_and_prune([spec], api=api.as_api(), environment="full", unit="bootstrap")

    async def test_a_correctly_labelled_object_applies(self) -> None:
        spec: Manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "root",
                "namespace": "argocd",
                "labels": {"ekn.dev/deployment-unit": "bootstrap"},
            },
        }
        api = FakeApi()

        await apply_and_prune([spec], api=api.as_api(), environment="full", unit="bootstrap", prune=False)

        assert api.patched == [("argocd", "ConfigMap", "root")]


class FakeCrd:
    """A CRD whose `wait` returns, or never does."""

    def __init__(self, *, ever_establishes: bool = True) -> None:
        self.name = "applicationsets.argoproj.io"
        self.ever_establishes = ever_establishes
        self.calls = 0

    async def wait(self, conditions: str) -> None:
        self.calls += 1
        if not self.ever_establishes:
            await anyio.sleep(3600)


class TestWaitEstablished:
    async def test_an_established_crd_is_waited_for_once(self) -> None:
        crd = FakeCrd()

        await _wait_established(cast("APIObject", crd), 5)

        assert crd.calls == 1

    async def test_a_crd_that_never_establishes_fails_with_its_name(self) -> None:
        """`anyio.fail_after` raises a bare `TimeoutError`, so
        `_wait_established` replaces it with one that names the CRD and the
        deadline.

        It also states what a cancel scope needs and a `timeout=` argument
        does not: that `kr8s.wait` passes a cancellation on rather than
        swallowing it. A callee that swallows one makes `fail_after` expire
        silently, and this test would hang instead.
        """
        crd = FakeCrd(ever_establishes=False)

        with pytest.raises(TimeoutError, match=r"applicationsets\.argoproj\.io did not become Established within"):
            await _wait_established(cast("APIObject", crd), 0.05)

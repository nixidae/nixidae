"""A kr8s `Api` double, for tests of code that applies and prunes.

Shipped with kappy rather than kept in its tests, because callers of the engine
test against the same double.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

import httpx
import kr8s
from kr8s.asyncio.objects import APIObject, new_class

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from kr8s.asyncio import Api

    from .apply import JsonValue, Resource

type Metadata = dict[str, JsonValue]


class _FakeResponse:
    # kr8s reads `Warning` off the headers of every apply response and logs
    # each one, so a fake with no `headers` raises rather than applying.
    def __init__(self, data: dict[str, JsonValue], headers: dict[str, str] | None = None) -> None:
        self._data = data
        self.headers = headers or {}

    def json(self) -> dict[str, JsonValue]:
        return self._data


def _selected(metadata: Metadata, selector: str | None) -> bool:
    """One `key=value` clause, which is all the sweep sends."""
    if not selector:
        return True
    key, _, value = selector.partition("=")
    labels = metadata.get("labels")
    return isinstance(labels, dict) and labels.get(key) == value


class FakeApi:
    """Stands in for kr8s's real Api for `apply_and_prune` tests.

    Two pieces of surface, and both are there to reproduce something real.

    `async_api_resources` is what `kappy.apply.discover` reads, so a test can
    describe a kind exactly as an API server would -- including one whose
    singular is not the lowercased Kind, which is the case kr8s' own lookup
    cannot resolve at all.

    `async_get` is implemented directly rather than going through kr8s's list
    machinery, so a test controls exactly what kind string comes back on a
    listed object. That reproduces kr8s' other quirk (see apply.py's comment
    on the prune loop): asked for a kind *by name*, `async_get_kind` reassigns
    its argument to `async_lookup_kind`'s `"singular.group/version"` string,
    which `new_class` mis-splits on the first ".", so listed objects report a
    lowercase `.kind` that differs from the PascalCase Kind used when
    applying. `apply_and_prune` passes the class instead, which is why the
    double accepts either.
    """

    namespace = "default"

    def __init__(
        self,
        *,
        namespaced: bool = True,
        listed: list[tuple[str, str, str]] | None = None,
        resources: list[Resource] | None = None,
        extra_metadata: dict[str, Metadata] | None = None,
        deny: dict[str, int] | None = None,
    ) -> None:
        self.namespaced = namespaced
        # Kinds whose LIST answers an error rather than a list, by status:
        # 403 for RBAC that covers writing a kind but not listing it, 405 for
        # the built-in kinds that have no list verb at all.
        self._deny = deny or {}
        # Merged into a listed object's `metadata`, keyed by its name. What
        # the prune guards read -- `ownerReferences` and the unit label --
        # lives there and nowhere else.
        self._extra_metadata = extra_metadata or {}
        # (namespace, kind-as-reported-by-list, name) triples "already on
        # the cluster" under the environment label before this apply.
        self._listed = listed or []
        # What the API server serves, in the shape `kappy.apply.discover` reads:
        # one entry per (Kind, groupVersion), carrying the plural and whether
        # the kind is namespaced. The default answers for the kind these tests
        # apply; a test about a hyphenated singular passes its own.
        self._resources: list[Resource] = (
            resources
            if resources is not None
            else [
                {
                    "version": "autoscaling.k8s.io/v1",
                    "kind": "VerticalPodAutoscaler",
                    "name": "verticalpodautoscalers",
                    "singularName": "verticalpodautoscaler",
                    "namespaced": namespaced,
                }
            ]
        )
        self.uncached_reads = 0
        self.deleted: list[tuple[str, str, str]] = []
        self.patched: list[tuple[str, str, str]] = []
        self.managers: dict[str, str] = {}
        self.swept: list[str] = []
        # (params, headers) of every LIST, for a caller that cares how it asked.
        self.list_requests: list[tuple[dict[str, str], dict[str, str]]] = []
        # Every object "on the cluster", keyed as the prune scan keys them and
        # holding the metadata a sweep would return. An apply adds one; the
        # `listed` objects were there before this run.
        self.live: dict[tuple[str, str, str], Metadata] = {}
        for ns, kind, name in self._listed:
            seen: Metadata = {
                "name": name,
                "namespace": ns,
                "resourceVersion": "1",
                "managedFields": [{"manager": "ekn"}],
            }
            self.live[(ns or "none", kind, name)] = seen | self._extra_metadata.get(name, {})

    @asynccontextmanager
    async def call_api(
        self,
        method: str,
        *,
        version: str | None = None,
        url: str | None = None,
        namespace: str | None = None,
        content: str | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> AsyncIterator[_FakeResponse]:
        if method == "GET":
            yield _FakeResponse(self._metadata_list(version, url, params or {}, headers or {}))
            return
        if method != "PATCH":
            raise AssertionError(f"FakeApi serves GET and PATCH, not {method}")
        body = json.loads(content or "{}")
        key: tuple[str, str, str] = (namespace or "none", body["kind"], body["metadata"]["name"])
        self.patched.append(key)
        # Which manager each object was applied as. SSA sends it as a query
        # parameter, so this is the only place a test can see it.
        self.managers[body["metadata"]["name"]] = (params or {}).get("fieldManager", "")
        # A real server answers an apply with the stored object, and only
        # *moves* the resourceVersion when the apply changed something. The
        # `--assume-unchanged` gate is exactly that behaviour, so a fake that
        # bumped on every apply would make the gate untestable.
        namespace_: str | None = body["metadata"].get("namespace")
        stored = self.live.setdefault(
            key,
            {
                "name": key[2],
                "namespace": namespace_,
                "resourceVersion": str(len(self.live) + 1),
            },
        )
        stored["labels"] = body["metadata"].get("labels", {})
        stored["managedFields"] = [{"manager": self.managers[body["metadata"]["name"]] or "ekn"}]
        yield _FakeResponse(body | {"metadata": body["metadata"] | {"resourceVersion": stored["resourceVersion"]}})

    def as_api(self) -> Api:
        """This double where an `Api` is wanted. It implements the surface the engine touches, not all of `Api`."""
        return cast("Api", self)

    def write(self, key: tuple[str, str, str]) -> None:
        """Somebody other than this apply wrote the object: a `kubectl edit`.

        Only the resourceVersion moves, which is the whole of what the gate
        reads and the whole of what such an edit is guaranteed to leave.
        """
        self.live[key]["resourceVersion"] = f"{self.live[key]['resourceVersion']}0"

    def _metadata_list(
        self,
        version: str | None,
        url: str | None,
        params: dict[str, str],
        headers: dict[str, str],
    ) -> dict[str, JsonValue]:
        """One kind's `ObjectMeta` list, recording the request for a caller to check."""
        self.list_requests.append((dict(params), dict(headers)))
        kind = next(
            (r["kind"] for r in self._resources if r["name"] == url and r["version"] == version),
            None,
        )
        self.swept.append(str(kind))
        if kind in self._deny:
            raise kr8s.ServerError(
                f"cannot list {kind}",
                response=httpx.Response(self._deny[str(kind)], request=httpx.Request("GET", str(url))),
            )
        selector = params.get("labelSelector")
        items: list[JsonValue] = [
            {"kind": "PartialObjectMetadata", "metadata": metadata}
            for (_ns, listed_kind, _name), metadata in self.live.items()
            if listed_kind == kind and _selected(metadata, selector)
        ]
        return {"kind": "PartialObjectMetadataList", "items": items}

    async def async_api_resources(self) -> list[Resource]:
        return self._resources

    async def async_api_resources_uncached(self) -> list[Resource]:
        self.uncached_reads += 1
        return self._resources

    def async_get(
        self,
        kind: str | type[APIObject],
        *,
        namespace: str | None,
        label_selector: str | dict[str, str] | None,
    ) -> AsyncIterator[APIObject]:
        # `apply_and_prune` passes a class for every kind: the ones it applied,
        # and the `prune_kinds` entries `_extra_classes` resolved. A string
        # would go through `async_lookup_kind` and mangle `.kind`.
        wanted = kind.kind if isinstance(kind, type) else kind

        async def _gen() -> AsyncIterator[APIObject]:
            record = self._record_delete
            reported_kind = wanted.lower() if wanted[0].isupper() else wanted
            for ns, listed_kind, name in self._listed:
                if listed_kind.lower() != reported_kind.lower():
                    continue
                base = new_class(listed_kind, "example.com/v1", namespaced=True)

                class Listed(base):  # type: ignore[valid-type,misc]  # `new_class` builds the base at runtime
                    async def delete(self, *args: object, **kwargs: object) -> None:
                        record(self.namespace, self.kind, self.name)

                obj = Listed(
                    {
                        "kind": listed_kind,
                        # `managedFields` naming `ekn` by default: a listed
                        # object stands for one a previous run applied, which
                        # is the only kind a prune is entitled to delete. A
                        # test about the manager guard overrides it.
                        "metadata": {
                            "name": name,
                            "namespace": ns,
                            "managedFields": [{"manager": "ekn"}],
                        }
                        | self._extra_metadata.get(name, {}),
                    },
                    api=cast("Api", self),
                )
                yield obj

        return _gen()

    def _record_delete(self, namespace: str | None, kind: str, name: str) -> None:
        self.deleted.append((namespace or "none", kind, name))

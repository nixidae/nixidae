"""Whether an object the API server accepted is in effect yet.

An apply that succeeds says the object is stored, not that it acts. The API
server loads an admission policy asynchronously, and a MutatingAdmissionPolicy
has no status that says it is loaded. Measured on solid-kubernetes' airlab
(fresh cluster, 2026-11-04): a policy and the Deployments it rewrites were
created in the same second, and the first pods kept their upstream images.
A server dry-run seconds later was rewritten. nixidae/nixidae#1175.

So an object may carry a probe: a request to server-dry-run, and what the
answer must show once the object acts. The apply holds every object ordered
after the probed one until the answer shows it.

A probe also catches a policy that never acts. Under `failurePolicy: Ignore`
a CEL error lets every request through unchanged, and the probe names that
instead of the pods quietly missing the policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

import anyio

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .apply import JsonValue, Manifest

PROBE_ANNOTATION: Final = "kappy.dev/probe"
"""How to see that this object is in effect, as a JSON object.

`{"object": <manifest>, "matches": <partial object>}` for a mutating
policy, or `{"object": <manifest>, "denied": "<substring>"}` for a
validating policy or a webhook. easykubenix renders it from `ekn.probe`.

**The probe goes on the last object of a set.** A policy acts only once its
binding exists, and the binding sorts after the policy
(`ekn.resourcePriority`). A probe on the policy holds its own binding back,
so it can never pass.

The probe object must be one the cluster would admit apart from the policy:
valid, allowed by Pod Security in its namespace, and in a namespace ordered
before the probed object. Its name must not exist: a dry-run against an
existing name is an UPDATE, which a CREATE-only policy ignores.
"""

DEFAULT_READY_SECONDS = 600.0
"""How long a probed object may take to come into effect.

A policy loads in seconds. A webhook acts only once its backing Deployment
serves, and on a fresh cluster that waits for image pulls: minutes. A bound
that is too short turns a slow first apply into a false failure, so this is
on the scale of `converge.DEFAULT_WAIT_SECONDS`.
"""

INITIAL_PROBE_INTERVAL = 0.5
MAX_PROBE_INTERVAL = 10.0


class DryRun(Protocol):
    """Server-dry-run a probe's request: the object the API server would store.

    Raises what the API server refused with, as an apply does. Raises
    `UnusableProbeError` when no answer could show the probed object at work.
    """

    def __call__(self, request: Manifest, /) -> Awaitable[Manifest]: ...


class UnusableProbeError(Exception):
    """The probe cannot show what it is for, whatever the cluster does.

    Not a "not yet": waiting does not change it, so the probed object fails
    at once.
    """


def project(actual: JsonValue, shape: JsonValue) -> JsonValue:
    """The parts of *actual* that *shape* names.

    Dicts keep the keys *shape* has. Lists pair by position, and the items
    *actual* has past the end of *shape* stay, so a length difference shows.
    Anything else is *actual* itself.
    """
    if isinstance(shape, dict) and isinstance(actual, dict):
        return {key: project(actual[key], value) for key, value in shape.items() if key in actual}
    if isinstance(shape, list) and isinstance(actual, list):
        return [project(item, like) for item, like in zip(actual, shape, strict=False)] + actual[len(shape) :]
    return actual


def _compact(value: JsonValue) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class Matches:
    """Admitted, and the stored object contains `partial`: a mutation acted.

    Containment is `project(response, partial) == partial`: every key
    `partial` names, recursively; lists item by item and of equal length.
    """

    partial: JsonValue

    def admitted(self, response: Manifest) -> str | None:
        got = project(response, self.partial)
        if got == self.partial:
            return None
        return f"expected {_compact(self.partial)}, got {_compact(got)}"

    def refused(self, message: str) -> str | None:
        return f"expected the request admitted with {_compact(self.partial)}, got refused: {message}"


@dataclass(frozen=True, slots=True)
class Denied:
    """Refused, with `substring` in the message: a validation acted.

    Pick a substring only the probed object says, such as its name. A Pod
    Security or schema refusal also refuses, and a generic word matches it.
    """

    substring: str

    def admitted(self, response: Manifest) -> str | None:
        del response
        return f"expected a refusal containing {self.substring!r}, got admitted"

    def refused(self, message: str) -> str | None:
        if self.substring in message:
            return None
        return f"expected a refusal containing {self.substring!r}, got: {message}"


type Expectation = Matches | Denied


@dataclass(frozen=True, slots=True)
class Probe:
    """A request to dry-run, and what the answer shows once the object acts."""

    request: Manifest
    expect: Expectation

    def describe(self) -> str:
        """`Kind/namespace/name` of the request, for a message."""
        metadata = self.request.get("metadata")
        meta = metadata if isinstance(metadata, dict) else {}
        parts = [str(self.request.get("kind", "?")), meta.get("namespace"), meta.get("name", "?")]
        return "/".join(str(part) for part in parts if part is not None)


def probe_of(spec: Manifest) -> Probe | None:
    """The probe *spec* carries in `PROBE_ANNOTATION`; None when it has none.

    Raises `ValueError` naming the problem when the annotation is malformed.
    """
    metadata = spec.get("metadata")
    annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
    raw = annotations.get(PROBE_ANNOTATION) if isinstance(annotations, dict) else None
    if raw is None:
        return None
    if not isinstance(raw, str):
        msg = "is not a string"
        raise ValueError(msg)
    try:
        parsed: JsonValue = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"is not JSON: {exc}"
        raise ValueError(msg) from exc
    if not isinstance(parsed, dict):
        msg = "is not a JSON object"
        raise ValueError(msg)
    request = parsed.get("object")
    if not (isinstance(request, dict) and isinstance(request.get("kind"), str)):
        msg = "needs `object`, a manifest with a kind"
        raise ValueError(msg)
    expectations = [key for key in ("matches", "denied") if parsed.get(key) is not None]
    if len(expectations) != 1:
        msg = f"needs exactly one of `matches` and `denied`, has {expectations or 'neither'}"
        raise ValueError(msg)
    if expectations == ["matches"]:
        return Probe(request, Matches(parsed["matches"]))
    denied = parsed["denied"]
    if not isinstance(denied, str) or not denied:
        msg = "`denied` is not a non-empty string"
        raise ValueError(msg)
    return Probe(request, Denied(denied))


async def check(probe: Probe, dry_run: DryRun) -> str | None:
    """Dry-run *probe* once: None when the object acts, else why not yet.

    Lets `UnusableProbeError` through. Every other failure is an answer to
    judge: a refusal is what `Denied` waits for, and for `Matches` an API
    server that is not ready yet is "not yet". The caller's deadline bounds
    both.
    """
    try:
        response = await dry_run(probe.request)
    except UnusableProbeError:
        raise
    except Exception as exc:  # noqa: BLE001 -- every refusal is an answer; see the docstring
        return probe.expect.refused(str(exc))
    return probe.expect.admitted(response)


class NotEffectiveError(TimeoutError):
    """A probed object did not come into effect in time."""


def probe_interval(attempt: int) -> float:
    return min(INITIAL_PROBE_INTERVAL * (2.0**attempt), MAX_PROBE_INTERVAL)


async def wait_effective(  # noqa: PLR0913 -- clock and sleep are injected so a test costs no wall time
    probe: Probe,
    dry_run: DryRun,
    *,
    what: str,
    seconds: float,
    clock: Callable[[], float] = anyio.current_time,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
) -> None:
    """Probe until the object named *what* acts, for up to *seconds*.

    A loop with a backoff, not a wait on an event: the API server sends no
    notice when it loads a policy.
    """
    deadline = clock() + seconds
    attempt = 0
    while True:
        verdict = await check(probe, dry_run)
        if verdict is None:
            return
        if clock() >= deadline:
            msg = f"{what} did not come into effect within {seconds:.0f}s: probe {probe.describe()}: {verdict}"
            raise NotEffectiveError(msg)
        await sleep(probe_interval(attempt))
        attempt += 1


__all__ = [
    "DEFAULT_READY_SECONDS",
    "PROBE_ANNOTATION",
    "Denied",
    "DryRun",
    "Expectation",
    "Matches",
    "NotEffectiveError",
    "Probe",
    "UnusableProbeError",
    "check",
    "probe_of",
    "project",
    "wait_effective",
]

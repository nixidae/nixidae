"""`channel.AgentRpc` against `agent.Agent`: one surface, written on two sides."""

from __future__ import annotations

import inspect

from vivarium_runner.agent import Agent
from vivarium_runner.channel import AgentRpc

PREFIX = "exposed_"


def declared() -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in vars(AgentRpc).items()
        if inspect.isfunction(member) and not name.startswith("_")
    }


def exposed() -> dict[str, inspect.Signature]:
    return {
        name.removeprefix(PREFIX): inspect.signature(member)
        for name, member in vars(Agent).items()
        if name.startswith(PREFIX)
    }


def test_the_protocol_names_every_exposed_method_and_no_other() -> None:
    assert set(declared()) == set(exposed())


def test_each_method_takes_the_same_parameters() -> None:
    agent = exposed()
    for name, signature in declared().items():
        assert list(signature.parameters) == list(agent[name].parameters), name


SYNC = {
    # Each reads /proc, which answers at once.
    "listening",
    "processes",
}


def test_a_handler_that_waits_is_async() -> None:
    """A sync handler runs on the agent's loop, and the channel reads nothing until it returns."""
    sync = {
        name.removeprefix(PREFIX)
        for name, member in vars(Agent).items()
        if name.startswith(PREFIX) and not inspect.iscoroutinefunction(member)
    }
    assert sync == SYNC

"""`huggorm.worker`: a server for one session, started `via` a transport
(nixidae/nixidae#551)."""

import pytest

from huggorm import Multiprocessing, ServerOptions, Stdio, UnixSocket, Via, attach, worker
from huggorm.server import _options, _parser
from huggorm.spawn import spawned

VIAS = [Stdio(), UnixSocket(), Multiprocessing("forkserver"), Multiprocessing("spawn")]


def test_the_command_line_carries_every_option() -> None:
    options = ServerOptions(
        settings={"system": "x86_64-test", "max-jobs": "3"},
        load_config=True,
        overlay="/r",
        lower_store="local://?root=/l",
        lower_dir="/l/nix/store",
        ttl=7.5,
        callbacks=True,
    )
    assert _options(_parser().parse_args(["--stdio", *options.argv()])) == options
    assert _options(_parser().parse_args(["--stdio"])) == ServerOptions()


async def test_a_worker_is_a_stdio_server_by_default() -> None:
    async with worker(store_uri="dummy://") as session:
        state = await session.eval(await session.store())
        assert await (await state.eval_expr("1 + 1")).integer() == 2


@pytest.mark.parametrize("via", VIAS, ids=repr)
async def test_every_transport_sets_the_server_up_from_its_options(via: Via) -> None:
    """The multiprocessing child takes the options as a value and the
    others as a command line; `system` shows that each one arrives."""
    options = ServerOptions(settings={"system": "x86_64-test"})
    async with worker(options, via=via, store_uri="dummy://") as session:
        state = await session.eval(await session.store())
        system = await state.eval_expr("builtins.currentSystem")
        assert await system.string_value() == "x86_64-test"


async def test_attach_reaches_a_server_something_else_started() -> None:
    async with spawned() as path:
        for _ in range(2):
            async with attach(path, store_uri="dummy://") as session:
                state = await session.eval(await session.store())
                assert await (await state.eval_expr("1 + 1")).integer() == 2

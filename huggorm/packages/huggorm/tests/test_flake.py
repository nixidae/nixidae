"""Locking and calling a flake (`flake.cc`), hermetically.

Every flake here is a `path:` flake in a temporary directory, and the
store is a chroot store beside it: `lockFlake` copies the source in,
and nothing reaches the network. The global registry is off, because
resolving any reference consults every registry layer and the default
global one is a URL.
"""

import json
import pathlib
import subprocess
from typing import Any

import pytest

from huggorm_bindings import EvalState, Store, parse_flake_ref
from huggorm_bindings.errors import UsageError
from huggorm_dsl.declare import NIX_2_35

if NIX_2_35:
    from huggorm_bindings import LazyAttr

pytestmark = pytest.mark.usefixtures("flakes")

NO_GLOBAL = {"flake-registry": ""}


def _flake(where: pathlib.Path, text: str) -> pathlib.Path:
    where.mkdir(parents=True)
    (where / "flake.nix").write_text(text)
    return where


@pytest.fixture
def state(tmp_path: pathlib.Path) -> EvalState:
    return EvalState(Store(str(tmp_path / "root")), NO_GLOBAL)


@pytest.fixture
def probe(tmp_path: pathlib.Path) -> pathlib.Path:
    dep = _flake(tmp_path / "src" / "dep", "{ outputs = _: { y = 1; }; }")
    return _flake(tmp_path / "src" / "probe", f"""{{
  description = "probe";
  inputs.dep.url = "path:{dep}";
  outputs = {{ dep, ... }}: {{ x = 7; y = dep.y; }};
}}""")


def test_a_locked_flake_calls_to_its_outputs(
        state: EvalState, probe: pathlib.Path) -> None:
    locked = state.lock_flake(parse_flake_ref(f"path:{probe}"))
    assert locked.description() == "probe"
    outputs = state.call_flake(locked)
    state.force(outputs)
    x, y = outputs.get("x"), outputs.get("y")
    state.force(x)
    state.force(y)
    assert (x.integer(), y.integer()) == (7, 1)


def test_the_lock_file_is_written_and_names_the_input(
        state: EvalState, probe: pathlib.Path) -> None:
    locked = state.lock_flake(parse_flake_ref(f"path:{probe}"))
    written = json.loads((probe / "flake.lock").read_text())
    assert written["nodes"]["root"]["inputs"] == {"dep": "dep"}
    dep = locked.find_input(["dep"])
    assert dep is not None
    assert dep.is_flake() is True
    assert dep.original_ref().to_attrs() == {
        "type": "path", "path": str(probe.parent / "dep")}
    assert dep.locked_ref().to_attrs()["type"] == "path"


def test_a_lock_that_is_not_written_leaves_no_file(
        state: EvalState, probe: pathlib.Path) -> None:
    state.lock_flake(parse_flake_ref(f"path:{probe}"), write_lock_file=False)
    assert not (probe / "flake.lock").exists()


def test_an_input_that_is_not_there_is_none(
        state: EvalState, probe: pathlib.Path) -> None:
    locked = state.lock_flake(parse_flake_ref(f"path:{probe}"))
    assert locked.find_input(["nope"]) is None
    assert locked.find_input([]) is None


def test_an_empty_input_path_is_refused(
        state: EvalState, probe: pathlib.Path) -> None:
    with pytest.raises(UsageError, match="must not be empty"):
        state.lock_flake(parse_flake_ref(f"path:{probe}"), update=[""])


def test_the_metadata_is_what_nix_flake_metadata_prints(
        state: EvalState, probe: pathlib.Path) -> None:
    locked = state.lock_flake(parse_flake_ref(f"path:{probe}"))
    metadata = json.loads(state.flake_metadata_json(locked))
    assert metadata["description"] == "probe"
    assert metadata["originalUrl"] == f"path:{probe}"
    assert metadata["path"].startswith("/nix/store/")
    assert set(metadata["locks"]["nodes"]) == {"root", "dep"}


def test_get_flake_answers_the_resolved_reference(
        state: EvalState, probe: pathlib.Path) -> None:
    resolved = state.get_flake(parse_flake_ref(f"path:{probe}"),
                               use_registries=False)
    assert resolved.to_attrs() == {"type": "path", "path": str(probe)}


def test_a_flake_setting_that_is_not_one_is_refused(
        state: EvalState, probe: pathlib.Path) -> None:
    with pytest.raises(UsageError, match="not a setting this call takes"):
        state.lock_flake(parse_flake_ref(f"path:{probe}"),
                         settings={"pure-eval": "true"})


def test_the_flake_builtins_are_there(state: EvalState) -> None:
    """`getFlake`, `parseFlakeRef` and `flakeRefToString`, which
    libflake adds to a state's settings rather than to every state."""
    ref = state.eval_expr(
        'builtins.flakeRefToString (builtins.parseFlakeRef '
        '"github:NixOS/nixpkgs/nixos-unstable")', "/")
    assert ref.string_value() == "github:NixOS/nixpkgs/nixos-unstable"
    assert state.eval_expr("builtins ? getFlake", "/").boolean()


def _commit(repo: pathlib.Path, text: str) -> str:
    """Commit *text* as the `flake.nix` of *repo*, and return the revision."""
    def git(*args: str) -> str:
        return subprocess.run(["git", "-c", "user.name=huggorm",
                               "-c", "user.email=huggorm@invalid", *args],
                              cwd=repo, check=True, capture_output=True,
                              text=True).stdout.strip()

    repo.mkdir(parents=True, exist_ok=True)
    (repo / "flake.nix").write_text(text)
    if not (repo / ".git").exists():
        git("init", "--quiet")
    git("add", "flake.nix")
    git("commit", "--quiet", "--message", text)
    return git("rev-parse", "HEAD")


@pytest.mark.parametrize("same_state", [False, True], ids=["new-state", "same-state"])
def test_a_second_lock_sees_a_new_commit(
        tmp_path: pathlib.Path, same_state: bool) -> None:
    """Nix keeps each git work tree's HEAD for the whole process
    (`getCachedWorkdirInfo`), and each fetched input for the life of a
    state (`inputCache`). So a second lock locked the old revision and
    read no `flake.lock` from the first. Each lock fetches anew."""
    dep = tmp_path / "dep"
    _commit(dep, "{ outputs = _: { y = 1; }; }")
    root = tmp_path / "root-flake"
    _commit(root, f"""{{
  inputs.dep.url = "git+file://{dep}";
  outputs = _: {{ }};
}}""")
    ref = parse_flake_ref(f"git+file://{root}")
    store = Store(str(tmp_path / "store"))
    first = EvalState(store, NO_GLOBAL)
    first.lock_flake(ref)
    moved = _commit(dep, "{ outputs = _: { y = 2; }; }")

    second = first if same_state else EvalState(store, NO_GLOBAL)
    locked = second.lock_flake(ref, update=["dep"])

    dep_node = locked.find_input(["dep"])
    assert dep_node is not None
    assert dep_node.locked_ref().to_attrs()["rev"] == moved
    written = json.loads((root / "flake.lock").read_text())
    assert written["nodes"]["dep"]["locked"]["rev"] == moved


def _locked_dep(tmp_path: pathlib.Path) -> Any:
    """A root flake with a git input of two commits, locked: the
    input's locked reference."""
    dep = tmp_path / "dep"
    _commit(dep, "{ outputs = _: { }; }")
    _commit(dep, "{ outputs = _: { y = 2; }; }")
    root = tmp_path / "root-flake"
    _commit(root, f"""{{
  inputs.dep.url = "git+file://{dep}";
  outputs = _: {{ }};
}}""")
    state = EvalState(Store(str(tmp_path / "store")), NO_GLOBAL)
    locked = state.lock_flake(parse_flake_ref(f"git+file://{root}"))
    node = locked.find_input(["dep"])
    assert node is not None
    return node.locked_ref()


def test_a_git_input_counts_its_revisions(tmp_path: pathlib.Path) -> None:
    """Nix 2.35's git fetcher gives `revCount` unforced, and the
    binding aborted the process on it (nixidae/nixidae#1048)."""
    ref = _locked_dep(tmp_path)
    count = ref.to_attrs()["revCount"]
    if NIX_2_35:
        assert isinstance(count, LazyAttr)
        assert count.force() == 2
    else:
        assert count == 2


def test_a_probe_flake_is_got(state: EvalState, probe: pathlib.Path) -> None:
    got = state.eval_expr(f'(builtins.getFlake "path:{probe}").x', "/")
    assert got.integer() == 7


async def test_a_flake_locks_over_the_socket(
        client: Any, tmp_path: pathlib.Path, probe: pathlib.Path) -> None:
    """`lock_flake` takes `settings: dict[str, str] | None`. None and a
    real mapping both cross (nixidae/nixidae#714)."""
    store = await client.acquire("Store", str(tmp_path / "remote"))
    state = await client.acquire("EvalState", store, NO_GLOBAL)
    locked = await state.lock_flake(parse_flake_ref(f"path:{probe}"))
    assert await locked.description() == "probe"
    with pytest.raises(UsageError, match="not a setting this call takes"):
        await state.lock_flake(parse_flake_ref(f"path:{probe}"),
                               settings={"pure-eval": "true"})

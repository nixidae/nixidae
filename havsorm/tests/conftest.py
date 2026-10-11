import pathlib
from typing import Any

import pytest
import salt.config
import salt.loader


@pytest.fixture
def opts(tmp_path: pathlib.Path) -> dict[str, Any]:
    """A minion's options with every directory in `tmp_path`."""
    config = salt.config.minion_config(None)
    config.update(
        file_client="local",
        root_dir=str(tmp_path),
        cachedir=str(tmp_path / "cache"),
        file_roots={"base": [str(tmp_path / "srv")]},
        grains={"os": "Leap", "id": "minion1"},
    )
    return config


@pytest.fixture
def srv(tmp_path: pathlib.Path) -> pathlib.Path:
    """The file_roots of `base`."""
    root = tmp_path / "srv"
    root.mkdir(exist_ok=True)
    return root


@pytest.fixture
def render_served(opts: dict[str, Any], tmp_path: pathlib.Path, srv: pathlib.Path) -> Any:
    """Render an SLS as a minion does: with the execution modules, from a
    copy in the cache. A file beside the SLS is on the fileserver only."""
    import io

    functions = salt.loader.minion_mods(opts)
    renderers = salt.loader.render(opts, functions, pillar={})

    def run(sls: str, rel: str) -> Any:
        text = (srv / rel).read_text()
        cached = tmp_path / "cache" / "files" / "base" / rel
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(text)
        return renderers["nix"](io.StringIO(text), saltenv="base", sls=sls, tmplpath=str(cached))

    return run


@pytest.fixture
def render(opts: dict[str, Any], tmp_path: pathlib.Path) -> Any:
    """Render Nix source as an SLS file through Salt's loader."""
    import io

    renderers = salt.loader.render(opts, {}, pillar={"role": "web"})

    def run(text: str, sls: str = "demo") -> Any:
        path = tmp_path / "srv" / f"{sls}.sls"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return renderers["nix"](io.StringIO(text), saltenv="base", sls=sls, tmplpath=str(path))

    return run

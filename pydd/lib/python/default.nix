{ pkgs, pydd }:
{
  /**
    A Python environment from a uv project, planned at build time.

    Takes the arguments of `./uv.nix`; `project` is the directory that holds
    `pyproject.toml` and `uv.lock`. Returns what `mkPlanner` returns, with
    `env`, `app`, `shell`, `environment` and `lockCheck` beside it.
  */
  uv = args: import ./uv.nix ({ inherit pkgs pydd; } // args);

  /**
    A Python environment from a Poetry project, planned at build time.

    Takes the arguments of `./poetry.nix`; `project` is the directory that
    holds `pyproject.toml` and `poetry.lock`, at lock version 2.1 or later.
    Returns what `mkPlanner` returns, with `env` beside it.
  */
  poetry = args: import ./poetry.nix ({ inherit pkgs pydd; } // args);
}

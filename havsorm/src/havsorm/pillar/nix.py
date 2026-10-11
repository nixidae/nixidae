"""A minion's pillar from a Nix file on the master.

    ext_pillar:
      - nix: /srv/nix-pillar/default.nix

or, with options:

    ext_pillar:
      - nix:
          file: /srv/nix-pillar/default.nix
          store: dummy://

The file evaluates to an attribute set, or to a function that takes
`{ id, grains, pillar, ... }` and returns one. `pillar` is what the
sources before this one gave. A relative path names a file beside the
file, on the master's disk.
"""

import os
from typing import Any

from salt.exceptions import SaltException

from havsorm.nixeval import evaluate


def ext_pillar(minion_id: str, pillar: dict[str, Any], *args: Any,
               file: str | None = None, store: str = "dummy://", **kwargs: Any) -> Any:
    path = file or (args[0] if args else None)
    if not path:
        raise SaltException("ext_pillar nix: name a file, as in `- nix: /srv/pillar.nix`")
    path = os.path.abspath(path)

    def load(state: Any, nix_store: Any) -> Any:
        return state.eval_file(path)

    data = evaluate(load, {"id": minion_id, "grains": dict(__grains__), "pillar": pillar},
                    store, path, SaltException)
    if not isinstance(data, dict):
        raise SaltException(f"ext_pillar nix: {path} gave {type(data).__name__}, not a set")
    return data

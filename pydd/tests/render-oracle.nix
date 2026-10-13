# What `lib/python/graph.nix` makes of a plan document that holds every node
# kind, for `test_render.py` to hold `pydd.python._render` to.
#
# The document carries the context that `_plan.document_value` attaches in a
# planner. `nix eval --read-only` computes each `.drvPath` and writes nothing.
let
  system = "x86_64-linux";
  names =
    list: f:
    builtins.listToAttrs (
      map (name: {
        inherit name;
        value = f name;
      }) list
    );

  # A store path with plain context, as a tool or a tree reaches a planner.
  source = name: builtins.toFile name name;

  # An artefact of the menu: the `.drv` with no context, and the output with
  # the context of its derivation.
  fetch =
    filename:
    let
      drv = derivation {
        name = filename;
        inherit system;
        builder = "/bin/sh";
        outputHashMode = "flat";
        outputHashAlgo = "sha256";
        outputHash = builtins.hashString "sha256" filename;
      };
    in
    {
      inherit filename;
      drv = builtins.unsafeDiscardStringContext drv.drvPath;
      out = "${drv}";
    };

  # The planner of one source distribution: a floating text output.
  planner = derivation {
    name = "flit-core-3.12.0.drv";
    inherit system;
    builder = "/bin/sh";
    __contentAddressed = true;
    outputHashMode = "text";
    outputHashAlgo = "sha256";
  };

  # The planner of a local project, which writes that project's node.
  localPlanner = derivation {
    name = "tool-editable.drv";
    inherit system;
    builder = "/bin/sh";
    __contentAddressed = true;
    outputHashMode = "text";
    outputHashAlgo = "sha256";
  };

  nixpkgs = derivation {
    name = "pyzmq-26.0.0";
    inherit system;
    builder = "/bin/sh";
  };

  tools = names [
    "bash"
    "coreutils"
    "unzip"
    "gnutar"
    "gzip"
    "python"
    "patchelf"
    "autoPatchelf"
    "bintools"
    "gccLib"
  ] tool;

  scripts = names [
    "unpackWheel"
    "installWheel"
    "buildSdist"
    "buildLocal"
    "makeVenv"
    "makeApplication"
  ] (name: source "script-${name}");

  # A tool as `planner.nix` hands one over: an output of a derivation, or a
  # source as `{ out }`.
  built = value: {
    drv = builtins.unsafeDiscardStringContext value.drvPath;
    out = "${value}";
    output = value.outputName;
  };
  package =
    name:
    derivation {
      inherit name system;
      builder = "/bin/sh";
      outputs = [
        "out"
        "lib"
      ];
    };
  tool =
    name:
    if name == "gccLib" then
      built (package "gcc").lib
    else if name == "patchelf" then
      { out = source "tool-patchelf"; }
    else
      built (package "tool-${name}");

  compiler = [
    (built (package "cc"))
    { out = source "zlib-dev"; }
  ];

  nodes = [
    {
      name = "installer-bootstrap";
      kind = "unpack";
      platform = false;
      artifact = fetch "installer-0.7.0-py3-none-any.whl";
    }
    {
      name = "rich";
      kind = "wheel";
      platform = false;
      artifact = fetch "rich-14.0.0-py3-none-any.whl";
    }
    {
      name = "psutil";
      kind = "wheel";
      platform = true;
      artifact = fetch "psutil-7.0.0-cp36-abi3-manylinux_2_17_x86_64.whl";
    }
    {
      name = "flit-sdist";
      kind = "sdist";
      platform = false;
      artifact = (fetch "flit_core-3.12.0.tar.gz") // {
        plannerDrv = builtins.unsafeDiscardStringContext planner.drvPath;
        plannerOut = "${planner}";
      };
    }
    {
      name = "idna";
      kind = "sdist-build";
      platform = false;
      artifact = (fetch "idna-3.10.tar.gz") // {
        subdirectory = "sub";
      };
      backend = "flit_core.buildapi";
      backendPath = [ "flit-sdist" ];
      buildTools = compiler;
    }
    {
      name = "app-editable";
      kind = "editable";
      platform = false;
      source = source "app-tree";
      root = "$PYDD_ROOT";
      backend = "flit_core.buildapi";
      backendPath = [ "flit-sdist" ];
      subdirectory = "";
      buildTools = compiler;
    }
    {
      name = "git-from-tree";
      kind = "local-wheel";
      platform = false;
      source = source "git-tree";
      backend = "setuptools.build_meta";
      backendPath = [ ];
      subdirectory = "python";
    }
    {
      name = "lib-sdist";
      kind = "local-sdist";
      platform = false;
      source = source "lib-tree";
      backend = "flit_core.buildapi";
      backendPath = [ "flit-sdist" ];
      subdirectory = "";
    }
    {
      name = "lib-from-sdist";
      kind = "sdist-build";
      platform = false;
      from = "lib-sdist";
      backend = "flit_core.buildapi";
      backendPath = [ "flit-sdist" ];
    }
    {
      name = "tool-editable";
      kind = "planned";
      platform = false;
      artifact = {
        plannerDrv = builtins.unsafeDiscardStringContext localPlanner.drvPath;
        plannerOut = "${localPlanner}";
      };
    }
    {
      name = "pyzmq-26.0.0-nixpkgs";
      kind = "nixpkgs";
      platform = false;
      artifact = {
        package = "pyzmq";
        version = "26.0.0";
        drv = builtins.unsafeDiscardStringContext nixpkgs.drvPath;
        out = "${nixpkgs}";
      };
    }
    {
      name = "app-env";
      kind = "venv";
      platform = false;
      members = [
        "rich"
        "psutil"
        "idna"
        "app-editable"
        "git-from-tree"
        "lib-from-sdist"
        "tool-editable"
        "pyzmq-26.0.0-nixpkgs"
      ];
      installed = [
        "rich==14.0.0"
        "psutil==7.0.0"
      ];
    }
    {
      name = "app";
      kind = "application";
      platform = false;
      venv = "app-env";
      bases = [ "app-editable" ];
    }
  ];

  document = {
    inherit
      system
      tools
      scripts
      nodes
      ;
    pythonVersion = "3.13";
    installer = "installer-bootstrap";
    root = "app";
  };

  graph = import ../lib/python/graph.nix;
  pathOf = value: if builtins.isAttrs value then value.drvPath else value;
in
{
  inherit document;
  expected = builtins.listToAttrs (
    map (node: {
      inherit (node) name;
      value = pathOf (graph (document // { root = node.name; }));
    }) nodes
  );
}

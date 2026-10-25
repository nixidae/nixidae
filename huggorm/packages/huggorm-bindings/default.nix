{
  lib,
  python3Packages,
  # The emitted C++ for this lane. setup.py compiles it and runs no
  # generator, so the census prints once, in its own log.
  bindings-src,
  # For its headers only.
  huggorm-decl,
  boehmgc,
  # The Nix libraries this binds, as components rather than the nix
  # package. `nix` is the CLI and drags its whole closure; a binding
  # needs libnixstore and libnixexpr and the two they rest on. Each
  # ships its own pkg-config file, which is what setup.py reads
  # (nixidae/nixidae#552). Separate arguments, so a caller with a component
  # scope of its own (nanopynix builds one per Nix version) passes
  # that scope's libraries.
  nix-util,
  nix-store,
  nix-expr,
  nix-fetchers,
  nix-flake,
  # For `lookupFileArg`, which resolves `<nixpkgs>`, `flake:x` and a
  # tarball URL as `nix eval --file` does. It lives in libcmd.
  nix-cmd,
  # `get-env.sh` as the `nix` binary embeds it.
  getEnvSh,
  # The licence texts of the libraries a wheel bundles, as
  # `licenses/<name>/<files>`, from the pinned sources.
  licenses,
  # The lane's `version`.
  version,
  pkg-config,
  ...
}:
let
  nixLibs = [
    nix-util
    nix-store
    nix-expr
    nix-fetchers
    nix-flake
    nix-cmd
  ];
in
python3Packages.buildPythonPackage {
  pname = "huggorm-bindings";
  version = "0.1.0";
  pyproject = true;
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./LICENSE
      ./NOTICE
      ./pyproject.toml
      ./setup.py
      ./_backend
    ];
  };

  build-system = with python3Packages; [
    setuptools
    nanobind
  ];

  env.HUGGORM_BINDINGS_EMITTED = bindings-src;
  env.HUGGORM_DECL_INCLUDE = "${huggorm-decl}/${python3Packages.python.sitePackages}";

  # build_ext compiles one extension at a time unless told otherwise.
  # setuptools reads DIST_EXTRA_CONFIG like a setup.cfg, so setup.py
  # stays free of Nix.
  preBuild = ''
    printf '[build_ext]\nparallel = %s\n' "$NIX_BUILD_CORES" > "$TMPDIR/build_ext.cfg"
    export DIST_EXTRA_CONFIG="$TMPDIR/build_ext.cfg"
  '';

  # pkg-config finds real Nix. It is how nix ships its build interface:
  # nix-store.pc carries -std=c++23 and a Requires chain that a
  # hand-written include path would have to reconstruct (nixidae/nixidae#552).
  nativeBuildInputs = [ pkg-config ];

  # boehmgc headers must be visible when compiling the extension.
  # `huggorm_decl/cpp/gc.hpp` calls GC_register_my_thread and GC_gcollect
  # directly - libexpr exposes no thread-registration API, so that half
  # of the integration is this repo's. null for a libexpr built without
  # the collector, and then nothing here names a Boehm symbol.
  # Not propagated: the extension reaches Nix's libraries through its
  # RPATH. A propagated `-dev` output puts its headers, boost's 147 MiB
  # among them, in every environment that imports the bindings
  # (nixidae/nixidae#939).
  buildInputs = lib.optional (boehmgc != null) boehmgc ++ nixLibs;

  # The lane's NixOS/nix release, which the declaration gates read. A
  # fork's `nix-store.version` is the fork's own.
  passthru.nixVersion = version;
  # Whether libexpr allocates through the collector: its tests ask
  # the build, never the binding they test.
  passthru.hasCollector = boehmgc != null;

  # Nix's own `src/nix/get-env.sh`, which `nix develop` runs as a
  # builder. Nix compiles it into the `nix` binary and no library
  # carries it, so the package that links the libraries carries the
  # text of the same version, as the binary embeds it.
  # NOTICE names whose terms the script travels under, and Nix's
  # `COPYING` is those terms: LGPL-2.1 section 1 has the licence text
  # go with every copy. `licenses/` holds the texts of the libraries a
  # wheel bundles, so the dev install carries what the wheel carries.
  postInstall = ''
    cp -f ${getEnvSh} \
      "$out/${python3Packages.python.sitePackages}/huggorm_bindings/get-env.sh"
    cp -f "${nix-store.src}/COPYING" \
      "$out/${python3Packages.python.sitePackages}/huggorm_bindings/COPYING.nix"
    cp -f ${./NOTICE} \
      "$out/${python3Packages.python.sitePackages}/huggorm_bindings/NOTICE"
    mkdir -p "$out/${python3Packages.python.sitePackages}/huggorm_bindings/licenses"
    cp -r ${licenses}/* \
      "$out/${python3Packages.python.sitePackages}/huggorm_bindings/licenses/"
  '';

  # Don't run `pip check` that might fail
  pythonImportsCheck = [ "huggorm_bindings" ];
}

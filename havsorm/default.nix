{
  sources ? import ./nix/sources.nix,
  # huggorm brings the nixpkgs and the Python every package here builds
  # on, so the bindings and havsorm share one interpreter.
  huggorm ? import sources.huggorm { inherit sources; },
}:
let
  pkgs = huggorm.pkgs.extend (
    final: prev: {
      # Salt 3008 requires xxhash (requirements/base.txt), and nixpkgs'
      # salt does not propagate it: salt-master fails to build its
      # resource index without it.
      salt = prev.salt.override { extraInputs = [ final.python3.pkgs.xxhash ]; };
    }
  );
  inherit (pkgs) lib;
  python3 = huggorm.python;
  # nixpkgs builds Salt as an application. The tests import it.
  salt = python3.pkgs.toPythonModule pkgs.salt;
in
rec {
  inherit huggorm pkgs salt;

  # nix build --file . havsorm
  #
  # Lints, then runs the suite: the loader finds the renderer through
  # the installed entry point, and `salt-call --local` renders an SLS.
  havsorm = python3.pkgs.buildPythonPackage {
    pname = "havsorm";
    version = "0.1.0";
    pyproject = true;
    src = lib.fileset.toSource {
      root = ./.;
      fileset = lib.fileset.unions [
        ./pyproject.toml
        ./README.md
        ./LICENSE
        ./src
        ./tests
      ];
    };
    build-system = [ python3.pkgs.setuptools ];
    dependencies = [ huggorm.huggorm ];
    nativeCheckInputs = [
      python3.pkgs.pytestCheckHook
      salt
      pkgs.ruff
    ];
    preCheck = ''
      ruff check --no-cache src tests
    '';
    # Not the renderer: it imports Salt, which runs havsorm and is not
    # one of its dependencies.
    pythonImportsCheck = [ "havsorm" ];
  };

  # The Salt bundle Uyuni and SUSE Multi-Linux Manager put on a client:
  # Salt 3006.0 on its own Python 3.11. The EL8 build, for PyPA's
  # manylinux_2_28 image. The repository holds one build at a time, so
  # this pin breaks when it moves.
  venvSaltMinion =
    let
      rpm = pkgs.fetchurl {
        url = "https://download.opensuse.org/repositories/systemsmanagement:/Uyuni:/Stable:/EL8-Uyuni-Client-Tools/EL_8/x86_64/venv-salt-minion-3006.0-80003.75.2.uyuni.x86_64.rpm";
        hash = "sha256-TNM+jG1eeFM7l4BD0KwUYP8ZOZyiQotrM5Yns+d0vKg=";
      };
    in
    pkgs.runCommand "venv-salt-minion-3006.0"
      {
        nativeBuildInputs = [
          pkgs.rpm
          pkgs.cpio
        ];
      }
      ''
        mkdir -p $out && cd $out
        rpm2cpio ${rpm} | cpio -idm --quiet
      '';

  # nix build --file . bundle
  #
  # havsorm in the Salt bundle, as `salt <minion> pip.install` puts it
  # there: pip into the directory the bundle's wrapper puts on
  # PYTHONPATH, from huggorm's cp311 wheels. Then the bundle's own
  # salt-call renders a #!nix state and a #!nix pillar SLS, each with a
  # relative import.
  bundle =
    let
      manylinux = huggorm.lanes.nix_2_34.manylinux;
      # The wheel without the suite: this gate and the suite each judge.
      wheel = (havsorm.overridePythonAttrs { doCheck = false; }).dist;
      venv = "${venvSaltMinion}/usr/lib/venv-salt-minion";
    in
    manylinux.build {
      pname = "havsorm-bundle";
      version = "3006.0";
      src = lib.fileset.toSource {
        root = ./.;
        fileset = lib.fileset.unions [
          ./tests/bundle
          ./formulas
        ];
      };
      script = ''
        # What the bundle's bin/python wrapper exports, with its paths in
        # the store and the pip target in /tmp.
        export PYTHONHOME=${venv} LD_LIBRARY_PATH=${venv}/lib PYTHONPATH=/tmp/local
        py=${venv}/bin/python.original
        $py -m pip install --quiet --no-index --target /tmp/local \
          --find-links ${manylinux.wheelhouse} --find-links ${wheel} havsorm
        root=$PWD/root
        mkdir -p conf "$root"
        printf '%s' '{"file_client": "local", "id": "bundle", "root_dir": "'"$root"'",
          "file_roots": {"base": ["'"$PWD"'/tests/bundle/srv", "'"$PWD"'/formulas/states"]},
          "pillar_roots": {"base": ["'"$PWD"'/tests/bundle/pillar"]},
          "grains": {"os": "Leap"}}' > conf/minion
        # Salt reads its user's passwd entry when it imports its config,
        # and the image has none for the sandbox's uid.
        call() {
          $py -c 'import os, pwd
        pwd.getpwuid = lambda uid: pwd.struct_passwd(("build", "x", uid, os.getgid(), "", "/tmp", "/bin/sh"))
        pwd.getpwnam = lambda name: pwd.getpwuid(os.getuid())
        import salt.scripts; salt.scripts.salt_call()' --local \
            --config-dir conf --out json --log-level warning "$@"
        }
        call --versions-report | tee "$out/versions.txt"
        call state.show_sls demo | tee "$out/state.json"
        call pillar.items | tee "$out/pillar.json"
        call state.show_sls nix-host | tee "$out/formula.json"
        $py - "$out" <<'EOF'
        import json, sys
        out = sys.argv[1]
        state = json.load(open(f"{out}/state.json"))["local"]
        assert list(state) == ["web", "db"], state
        assert {"port": 8080, "os": "Leap"} in state["web"]["test"], state
        pillar = json.load(open(f"{out}/pillar.json"))["local"]
        assert pillar.get("dbPort") == 5432, pillar
        formula = json.load(open(f"{out}/formula.json"))["local"]
        assert list(formula) == ["nix", "nix-profile", "nix-store-gc"], formula
        assert {"version": "2.34.6"} in formula["nix"]["havsorm"], formula
        assert {"require": [{"havsorm": "nix"}]} in formula["nix-store-gc"]["havsorm"], formula
        print("bundle: #!nix state, pillar and the nix-host formula render on Salt 3006.0, Python 3.11")
        EOF
      '';
    };

  # Salt with havsorm, for a minion or a master: bin/salt-minion and the
  # rest run on this Python.
  saltEnv = python3.withPackages (_: [
    (havsorm.overridePythonAttrs { doCheck = false; })
    salt
  ]);

  # The NixOS module that runs nixpkgs' Salt services from `saltEnv`.
  nixosModule = ./nix/nixos.nix;

  # nix build --file . fleet
  #
  # A master, a NixOS minion and an openSUSE minion on vivarium.
  fleet = import ./nix/fleet.nix {
    inherit pkgs saltEnv;
    vivarium = import (sources.vivarium + "/lib.nix") { inherit pkgs; };
  };

  # nix run --file . python -- -m pytest
  #
  # Without the suite, so a red suite does not block the loop that
  # debugs it.
  python = python3.withPackages (_: [
    (havsorm.overridePythonAttrs { doCheck = false; })
    salt
    python3.pkgs.pytest
  ]);
}

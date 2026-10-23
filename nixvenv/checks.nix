{
  runCommand,
  python3,
  nixvenv,
}:
let
  venv = nixvenv {
    python = python3;
    # pytest has a console script; typing-extensions is the module that,
    # on PYTHONPATH, shadows a type checker's stub (nixidae/nixidae#1046).
    packages = ps: [
      ps.pytest
      ps.typing-extensions
    ];
    executables = {
      python = null;
      pytest = "venv-pytest";
    };
  };
in
{
  # What a venv promises, measured from inside it and from a child of it.
  venv = runCommand "nixvenv-check-venv" { } ''
    set -eu
    unset PYTHONPATH
    venv=${venv}/lib/venv
    ${venv}/bin/python - "$venv" <<'EOF'
    import os, subprocess, sys
    venv = sys.argv[1]
    def check(ok, what):
        if not ok:
            sys.exit(f"nixvenv: {what}")
    check(sys.prefix == venv, f"sys.prefix is {sys.prefix}, not {venv}")
    check(sys.prefix != sys.base_prefix, "not a venv: prefix equals base_prefix")
    check(sys.executable.startswith(venv + "/bin/"), f"sys.executable is {sys.executable}")
    check("PYTHONPATH" not in os.environ, "PYTHONPATH is set")
    import typing_extensions
    check(typing_extensions.__file__.startswith(venv), f"typing_extensions from {typing_extensions.__file__}")
    child = subprocess.run(
        [sys.executable, "-c", "import sys, pytest; print(sys.prefix)"],
        capture_output=True, text=True, check=True,
    )
    check(child.stdout.strip() == venv, f"a child of sys.executable starts in {child.stdout.strip()}")
    EOF
    ${venv}/bin/venv-pytest --version
    touch $out
  '';
}

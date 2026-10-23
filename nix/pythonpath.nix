# That no source sets PYTHONPATH (nixidae/nixidae#1046). A tool that reads
# PYTHONPATH, such as a type checker, sees another world than the
# interpreter. A program takes a venv: nixvenv, or a pyproject.nix
# `mkVirtualEnv`. A file that still sets it is in `allowed`, with the reason.
#
# A read, an `unset` or a filter that drops the variable does not match.
{
  lib,
  runCommand,
  sources,
}:
let
  members = lib.attrNames (import ./members.nix);
  allowed = {
    # The Salt bundle's own bin/python wrapper exports it; the test copies
    # that environment.
    "havsorm/default.nix" = "third-party environment";
    # nixidae/nixidae#866: the beartype shim reaches subprocesses this way.
    "nanopynix/nanopynix-testing/src/nanopynix_testing/beartype_hook.py" = "#866";
    # pytest-agent supports a run from PYTHONPATH with no install metadata.
    "nanopynix/pytest-agent/tests/conftest.py" = "a supported install mode";
  };
  pattern = ''(^|[^A-Za-z0-9_])PYTHONPATH("\]?)?[[:space:]]*=[^=]|(^|[^A-Za-z0-9_])PYTHONPATH"[[:space:]]*:|setenv\("PYTHONPATH"'';
in
runCommand "nixidae-pythonpath" { } ''
  fail=0
  ${lib.concatMapStrings (name: ''
    cd ${sources.${name}}
    while IFS=: read -r file line text; do
      case "${name}/''${file#./}" in
        ${lib.concatStrings (map (path: "${path}) ;;\n") (lib.attrNames allowed))}
        *) echo "${name}/''${file#./}:$line sets PYTHONPATH: $text" >&2; fail=1 ;;
      esac
    done < <(grep -rInE ${lib.escapeShellArg pattern} --exclude='*.md' . || true)
  '') members}
  [ "$fail" = 0 ] && touch $out
''

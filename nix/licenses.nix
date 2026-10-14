# That every source is Apache-2.0. Its LICENSE is the Apache text, and an
# SPDX header in a file of its own names Apache-2.0. A file that keeps
# another project's terms is in `others`, with the reason.
{
  lib,
  runCommand,
  sources,
}:
let
  members = lib.attrNames (import ./members.nix);
  others = {
    # A port of NixOS/nix#5265, so Nix's terms.
    "huggorm/packages/nix-tcp-store/tcp-store.cc" = "LGPL-2.1-or-later";
  };
in
runCommand "nixidae-licenses" { } ''
  fail=0
  ${lib.concatMapStrings (name: ''
    if ! grep -q 'Apache License' ${sources.${name}}/LICENSE 2>/dev/null \
       || ! grep -q 'Version 2.0, January 2004' ${sources.${name}}/LICENSE; then
      echo "${name}/LICENSE is not the Apache-2.0 text" >&2
      fail=1
    fi
    cd ${sources.${name}}
    while IFS=: read -r file id; do
      id=$(echo "$id" | sed -E 's/.*SPDX-License-Identifier:[[:space:]]*//; s/[[:space:]]*(\*\/)?$//')
      case "${name}/''${file#./}=$id" in
        *=Apache-2.0) ;;
        ${lib.concatStrings (lib.mapAttrsToList (path: id: "${path}=${id}) ;;\n") others)}
        *) echo "${name}/''${file#./} names $id" >&2; fail=1 ;;
      esac
    done < <(grep -rIEo -m1 '^[[:space:]]*(#|//|--|/\*)[[:space:]]*SPDX-License-Identifier:.*' . || true)
  '') members}
  [ "$fail" = 0 ] && touch $out
''

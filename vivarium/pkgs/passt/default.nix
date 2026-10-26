/**
  passt, patched so that `--address`, `--netmask` and `--gateway` hold in
  passt's local mode too.

  A Nix build sandbox has no interface with a default route, so passt
  enters local mode and, unpatched, hands the guest 169.254.2.1/16 via
  169.254.2.2 whatever vivarium asked for: a sandboxed run and an
  interactive one gave a guest different uplinks (measured on the sg1
  topology test, 2026-10-26).
*/
{ passt }:
passt.overrideAttrs (old: {
  patches = (old.patches or [ ]) ++ [ ./local-mode-keeps-given-addresses.patch ];
})

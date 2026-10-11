#!nix
# The nix-host formula: Nix installed, an optional profile, and a free
# space floor, from the form's pillar. A list keeps the states in order.
{ pillar, ... }:
let
  form = pillar.nix_host or { };
  version = form.version or "";
  profile = form.profile or { };
  needNix = [ { require = [ { havsorm = "nix"; } ]; } ];
in
[
  {
    nix."havsorm.store_present" = [
      { settings.experimental-features = form.experimental_features or "nix-command flakes"; }
    ] ++ (if version == "" then [ ] else [ { inherit version; } ]);
  }
]
++ (if (profile.path or "") == "" then [ ] else [
  {
    nix-profile."havsorm.profile" = [
      { name = profile.name or "/nix/var/nix/profiles/salt"; }
      { inherit (profile) path; }
    ] ++ needNix;
  }
])
++ [
  {
    nix-store-gc."havsorm.garbage_collected" = [
      { keep_free = form.keep_free or "10G"; }
    ] ++ needNix;
  }
]

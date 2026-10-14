#!nix
# `salt-run event.send havsorm/release/<name> '{minion: ..., path: ...}'`
{ data, ... }:
let
  release = data.data or data;
in
{
  "deploy-${release.minion}"."local.state.single" = [
    { tgt = release.minion; }
    {
      args = [
        { fun = "havsorm.system"; }
        { name = release.path; }
      ];
    }
  ];
}

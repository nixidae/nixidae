#!nix
# A rolling deploy of NixOS systems:
#
#   salt-run state.orchestrate orch.deploy pillar='{systems: {<id>: <path>}, good: {<id>: <path>}}'
#
# One state per minion, each requiring the one before, so a failure
# stops the rest. Then, only on a failure, every minion goes back to its
# system in `good`.
#
# Not `batch` with `failhard`: Salt reads `failhard` on a salt.state as
# the orchestration's own failhard too (state.py check_failhard), which
# ends the run before the `onfail` restore.
{ pillar, ... }:
let
  inherit (builtins) attrNames concatStringsSep genList elemAt length;
  ids = attrNames pillar.systems;
  deploy = systems: tgt: [
    { inherit tgt; }
    { tgt_type = "list"; }
    { sls = [ "deploy" ]; }
    { pillar.systems = systems; }
  ];
  step =
    i:
    let
      id = elemAt ids i;
    in
    {
      "deploy-${id}"."salt.state" =
        deploy { ${id} = pillar.systems.${id}; } id
        ++ (if i == 0 then [ ] else [ { require = [ { salt = "deploy-${elemAt ids (i - 1)}"; } ]; } ]);
    };
in
genList step (length ids)
++ (
  if pillar ? good then
    [
      {
        restore."salt.state" = deploy pillar.good (concatStringsSep "," (attrNames pillar.good)) ++ [
          { onfail = map (id: { salt = "deploy-${id}"; }) ids; }
        ];
      }
    ]
  else
    [ ]
)

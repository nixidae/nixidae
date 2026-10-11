#!nix
# Each minion's system, from the pillar the orchestration passes.
{ grains, pillar, ... }:
{
  system."havsorm.system" = [ { name = pillar.systems.${grains.id}; } ];
}

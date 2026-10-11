#!nix
{ grains, ... }:
let ports = import ./ports.nix; in
[
  { web."test.nop" = [ { port = ports.http; os = grains.os; } ]; }
  { db."test.nop" = [ ]; }
]

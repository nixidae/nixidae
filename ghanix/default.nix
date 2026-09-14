# ghanix -- render GitHub Actions workflows from a NixOS-module-system
# schema, evaluated down to plain YAML-able values.
#
# It takes `lib` and nothing else: nixpkgs' `lib`, for `lib.evalModules`,
# `lib.types` and `lib.asserts`. No flake, no store path, no nixpkgs source,
# no project plumbing. That is why it is a repository rather than a
# directory, and it is the property to keep.
#
# Grown inside nanopynix, which is still its first consumer. The header here
# used to say the split "would let this directory be lifted into its own
# project unchanged". It was, and it was: these three files moved without an
# edit to any of them.
#
# Usage:
#   ghalib = import sources.ghanix { inherit lib; };
#   ghalib.evalWorkflow {
#     name = "On commit";
#     on = { push = { }; };
#     jobs.build.steps = [ (ghalib.steps.checkout { }) ];
#   }
#
# `evalWorkflow` returns an attrset, not text. Turning that into a file is
# the caller's job, because the two consumers do it differently and neither
# way belongs here: nanopynix renders through its own `to_yaml` for key
# order it controls, and nixkube writes it with `pkgs.formats.yaml`. YAML is
# a superset of JSON, so `builtins.toJSON` into a `.yml` file also works and
# needs no builder at all.
{ lib }:
let
  schema = import ./schema.nix { inherit lib; };
  steps = import ./steps.nix { inherit lib; };
in
schema // steps

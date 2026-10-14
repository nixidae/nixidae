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
# `evalWorkflow` returns an attrset, not text. Writing the file is still the
# caller's job, because the callers disagree about the gate and about the
# formatter: nanopynix compares in pytest and rewrites, nixkube runs yamlfmt
# because treefmt owns the committed file. They do not disagree about the
# writer, so `toYamlScript` is here and the two halves around it are not.
#
# YAML is a superset of JSON, so `builtins.toJSON` into a `.yml` file also
# works and needs no builder at all. It is unreadable in a diff.
{ lib }:
let
  schema = import ./schema.nix { inherit lib; };
  steps = import ./steps.nix { inherit lib; };
in
schema
// steps
// {
  # A path, and not a store path: ghanix takes `lib` and nothing else, and
  # keeping that true is what lets a repository use it while pinning its own
  # nixpkgs. A caller interpolates it into a builder, which is where it
  # becomes a store path.
  toYamlScript = ./to_yaml.py;
}

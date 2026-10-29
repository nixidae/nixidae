# Getting started

`lib.evalWorkflow` takes a workflow as Nix and returns it as a value.
`lib.toYamlScript` writes that value as YAML.

## Write a workflow

Put this in `ci.nix`:

```nix
{ pkgs ? import <nixpkgs> { } }:
let
  ghalib = (import (builtins.fetchTarball "https://github.com/nixidae/ghanix/archive/main.tar.gz") {
    inherit (pkgs) lib;
  }).lib;
  workflow = ghalib.evalWorkflow {
    name = "Build";
    on.push = { };
    jobs.build = {
      runs-on = "ubuntu-latest";
      ghanix.checkout.enable = true;
      ghanix.nix.install.enable = true;
      steps = [ { run = "nix build --file ."; } ];
    };
  };
in
pkgs.runCommand "build.yml" {
  nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.pyyaml ])) ];
  value = builtins.toJSON workflow;
  passAsFile = [ "value" ];
} ''python3 ${ghalib.toYamlScript} "$valuePath" > $out''
```

`ghanix.checkout` and `ghanix.nix.install` add the checkout and the Nix
installation as the first steps of the job. A step that makes room on the
disk comes between them, because `ghanix.freeDiskSpace` is on by default.

## Render it

```console
$ nix build --file ci.nix
$ cp result .github/workflows/build.yml
```

A key that the workflow level does not know is an evaluation error. A job
and a step accept any key, so GitHub can add keys without a change here.

[How to use](usage.md) explains the steps a job asks for by name.
[Workflow options](options.md) lists every option of the schema.

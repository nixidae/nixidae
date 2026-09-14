# ghanix

Write GitHub Actions workflows as NixOS modules, and get back plain values
that a caller turns into YAML.

```nix
ghalib = import sources.ghanix { inherit lib; };

ghalib.evalWorkflow {
  name = "On commit";
  on.push = { };
  jobs.build.steps = [
    (ghalib.steps.checkout { })
    (ghalib.steps.installNix { })
  ];
}
```

## What it gives you

`evalWorkflow` runs the attrset through `lib.evalModules` against a schema of
the workflow, job and step shapes, then strips nulls and returns the result.
A declared key gets a type, a default and a description, and the workflow
level is closed: a key it does not know is an evaluation error.

Jobs and steps are **not** closed. Both carry
`freeformType = types.attrsOf types.anything`, so GitHub grows a key and
nothing here has to be taught it. The cost is that a typo passes:

```nix
jobs.a.steps = [ { run = "true"; tiemout-minutes = 5; } ];
# renders "tiemout-minutes": 5, and GitHub ignores it
```

So this is not a spell-checker. What it buys is that a workflow is *code*:
one definition of a job shared by two variants, a step list built by a
function, a value computed once instead of pasted into four places. The
schema is there to give the common keys a type and a sane default, not to
police the rest.

`steps` holds constructors for the actions that appear in every repository:
checkout, install Nix, upload and download one artifact. Each carries a
default `timeout-minutes`, generous against the measured time, because the
cap is there to catch a step that stopped rather than a step that is slow.

`withCond` and `withTimeout` wrap a step or a job, because `if` is a Nix
keyword and cannot be a formal argument name.

## What it does not give you

Text. `evalWorkflow` returns an attrset and the caller writes the file,
because the two consumers disagree about how and neither way belongs in a
library:

- **nanopynix** renders through its own `to_yaml`, for key ordering it
  controls, and a pytest gate compares the result against the checked-in
  YAML and rewrites it when they differ.
- **nixkube** writes it with `pkgs.formats.yaml`.

YAML is a superset of JSON, so `builtins.toJSON` written to a `.yml` file is
also a valid workflow and needs no builder at all. It is unreadable in a
diff, which is usually the reason not to.

## Requirements

`lib` from nixpkgs. Nothing else -- no flake, no store path, no nixpkgs
source. Keeping that true is what makes this usable from a repository that
pins its own nixpkgs, or none.

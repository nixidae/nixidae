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
checkout, install Nix, cachix, upload and download one artifact, the three
that publish a docs tree to Pages, and the two that change what a runner
will let a job do. Each carries a default `timeout-minutes`, generous
against the measured time, because the cap is there to catch a step that
stopped rather than a step that is slow.

`withCond` and `withTimeout` wrap a step or a job, because `if` is a Nix
keyword and cannot be a formal argument name.

## Steps a job asks for by name

The steps at the front of a job are the same in every repository, and
writing them out is how they drift. A job can enable them instead:

```nix
jobs.test = {
  ghanix = {
    checkout.enable = true;
    nix.install = {
      enable = true;
      settings.trusted-users = [ "root" "runner" ];
    };
    nix.cachix.enable = true;
    userNamespaces.enable = true;   # the Nix sandbox, and passt
    openKvm.enable = true;          # x64 runners only
    freeDiskSpace.enable = true;
  };
  steps = [ { run = "nix build --file . thing"; } ];
};
```

Each one contributes to `steps` through `lib.mkOrder`, below the 1000 a
plain list definition gets, so they come first and in a fixed order:
checkout, room on the disk, Nix, cachix, then the runner's own permissions.
Everything the job wrote for itself follows.

`nix.install.settings` is an attrset of nix.conf keys, where a list joins on
spaces. That is what lets a project with a cache of its own drop the
composite action it would otherwise need to carry substituters -- and a
composite action cannot hold `timeout-minutes` at all.

`nix.install.uidRange = true` (or `steps.installNix { uidRange = true; }`)
offers the `uid-range` system feature: `auto-allocate-uids`, `use-cgroups`,
and the feature added through `extra-system-features`, so the runner keeps
the ones Nix detects. A build that asks for it runs as root with 65536 ids
and a cgroup of its own, which systemd in a container needs.

`ghanix` is stripped from each job before rendering. It is one attribute
for that reason: a job is freeform, so any other key it carries goes to
GitHub verbatim, and a key GitHub does not know makes it refuse to load the
whole workflow -- which no render gate here would catch.

## What it does not give you

A file. `evalWorkflow` returns an attrset, and the caller decides where it
lands, what gates it and what formats it:

- **nanopynix** renders every workflow in one call with `ci/render.py`,
  which separates rendering from writing so its pytest gate can compare
  without touching disk — the packaged CI runner runs from a read-only
  store copy. It compares against the checked-in YAML and rewrites it when
  the two differ.
- **nixkube** runs the render through yamlfmt, because treefmt formats the
  committed file and its CI ends in `git diff --exit-code`. A gate
  derivation parses the committed YAML and compares it against the value,
  so style never fails it.
- **pynixd** does neither: it has no YAML formatter, so the render is the
  only thing that writes the file.

The writer itself **is** here, because that part they agreed about.
`toYamlScript` is the path of a script that takes a JSON file and prints
YAML: a literal block for every multi-line string, `'on'` quoted because
YAML 1.1 reads a bare `on` as the boolean `true`, and no line wrapping.

```nix
pkgs.runCommand "ci.yml" {
  nativeBuildInputs = [ (pkgs.python3.withPackages (ps: [ ps.pyyaml ])) ];
  value = builtins.toJSON workflow;
  passAsFile = [ "value" ];
} ''python3 ${ghalib.toYamlScript} "$valuePath" > $out''
```

It is a path and not a store path, so ghanix still needs `lib` and nothing
else. nixkube and pynixd held identical copies of it and nothing made them
agree; issue #1 has the detail.

YAML is a superset of JSON, so `builtins.toJSON` written to a `.yml` file is
also a valid workflow and needs no builder at all. It is unreadable in a
diff, which is usually the reason not to.

## Requirements

`lib` from nixpkgs. Nothing else -- no flake, no store path, no nixpkgs
source. Keeping that true is what makes this usable from a repository that
pins its own nixpkgs, or none.

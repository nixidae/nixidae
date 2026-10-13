{ pydd, writeText }:
{
  /**
    A planner whose graph is a Nix expression, evaluated in the sandbox.

    `expression` is a function of `args`, and returns the root derivation.
    A store path in `args` arrives with its context, so a derivation the
    expression writes may read it. A candidate (`pydd.candidate`) arrives as
    `{ drv, outputs }`, and reading one of its outputs builds it.

    Needs huggorm, which attaches that context. Takes everything
    `mkPlanner` takes but `plan`, `menu` and `writer`.
  */
  mkEvaluated =
    {
      expression,
      args ? { },
      ...
    }@attrs:
    pydd.mkPlanner (
      removeAttrs attrs [
        "expression"
        "args"
      ]
      // {
        writer = "huggorm";
        plan = writeText "plan.py" "from pydd.modes.evaluate import main\nmain()\n";
        modules = [
          ../src/pydd/modes/__init__.py
          ../src/pydd/modes/evaluate
        ]
        ++ attrs.modules or [ ];
        menu = {
          expression = "${expression}";
          inherit args;
        };
      }
    );
}

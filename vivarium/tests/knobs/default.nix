/*
  What is a run told from outside, and what is a check told instead?

  A knob is resolved while evaluating, so it can change what is built
  -- a phase order, a guest's memory, an image -- which nothing read
  at run time can do. The price is that setting one moves the
  derivation, and that is the trade this records rather than hides.

  The property under test is the half that keeps CI honest: inside a
  sandbox a knob always carries its declared default, because
  `builtins.getEnv` answers "" under a pure evaluation and an unset
  variable answers "" too. So an exported variable cannot make the
  check run something other than the check.

  Also asserts a knob is not ambient. Nothing in the guest's
  environment carries it; a phase hands it over or the guest never
  sees it, which keeps a guest's behaviour a function of its own
  configuration.
*/
{ mkTest, ... }:
mkTest {
  name = "knobs";
  nodes.one = { };
  knobs.selection = {
    env = "VIVARIUM_SELECTION";
    default = "every-case";
    description = "Which cases to run; the default is all of them.";
  };
  phases = {
    boot.script = ../phases/boot.py;
    knob = {
      script = ../phases/knob.py;
      after = [ "boot" ];
    };
  };
}

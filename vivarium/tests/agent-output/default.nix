# Does the agent channel carry large answers, one after another?
# nixidae/nixidae#1178: a QEMU guest stopped answering at the third.
{ mkTest, ... }:
mkTest {
  name = "agent-output";
  backend = "qemu";
  nodes.one = { };
  phases.output = {
    script = ../agent-output.py;
    after = [ "boot" ];
    timeout = 300;
  };
}

/*
  Does every check run in a CI job, or name the reason in `byHand` in
  ci/workflows.nix? No guest boots: tests/checks/ci_set.py compares the
  names.
*/
{
  hostPkgs,
  ciSet,
  checkLeaves,
  ...
}:
hostPkgs.runCommand "vivarium-check-ci-set"
  {
    ciSet = builtins.toJSON {
      leaves = checkLeaves;
      inherit (ciSet) attrs byHand;
    };
    passAsFile = [ "ciSet" ];
  }
  ''
    ${hostPkgs.python3.interpreter} ${../checks/ci_set.py} "$ciSetPath"
    touch $out
  ''

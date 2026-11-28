/*
  Does examples/k8s-devenv come up a second time, as `vivarium down` and
  `vivarium up` bring it? Two runs into one environment directory. The
  second resumes the kept cluster, with `persist`, `loadBalancer` and
  `certManager` together, and every phase passes again: the
  LoadBalancer controller reconnects, and the issuer signs with the
  second run's intermediate CA. ../checks/k8s-devenv-kept.py.
*/
{
  checkScript,
  pkgs,
  lib,
  hostPkgs,
  mkTest,
  ...
}:
let
  run = import ../../examples/k8s-devenv {
    inherit
      mkTest
      lib
      pkgs
      hostPkgs
      ;
  };
in
pkgs.runCommand "vivarium-check-k8s-devenv-kept" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${checkScript "k8s-devenv-kept.py"} ${lib.getExe run.driver} "$TMPDIR"
  touch $out
''

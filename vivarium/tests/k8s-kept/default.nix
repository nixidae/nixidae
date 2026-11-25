/*
  Does a Kubernetes environment keep its cluster across runs? Two runs
  into one environment directory: the first makes the cluster and a
  ConfigMap, the second resumes it and finds the ConfigMap. The control
  plane keeps its cluster (`persist`) and the worker keeps nothing, so the
  second run also joins the worker in place of its old Node.
  tests/checks/k8s-kept.py, nixidae/nixidae#1588.

  10.111: every other cluster test has its own subnet.
*/
{
  checkScript,
  pkgs,
  lib,
  k8sNodes,
  mkTest,
  ...
}:
let
  node = name: address: {
    imports = [ k8sNodes.${name} ];
    services.vivarium-k8s.persist = name == "cp";
    vivarium.lan = lib.mkForce {
      network = "k8s-kept";
      inherit address;
    };
  };
  run = mkTest {
    name = "k8s-kept";
    vivarium.recipes.kubernetes.enable = true;
    phases.up = {
      script = ../k8s-kept.py;
      after = [ "boot" ];
    };
    nodes.cp = node "cp" "10.111.0.1/24";
    nodes.worker1 = node "worker1" "10.111.0.2/24";
  };
in
pkgs.runCommand "vivarium-check-k8s-kept" { passthru.session = run; } ''
  ${pkgs.python3.interpreter} ${checkScript "k8s-kept.py"} ${lib.getExe run.driver} "$TMPDIR"
  touch $out
''

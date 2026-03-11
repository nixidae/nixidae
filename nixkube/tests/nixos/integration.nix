# SPDX-License-Identifier: MIT

# NixOS integration test for nixkube.
# Spins up a single-node kubeadm + containerd cluster, deploys nixkube,
# runs CSI and NRI test workloads, and verifies they complete.
#
# Usage from default.nix:
#   nixosTests.containerd = import ./tests/nixos/integration.nix {
#     inherit pkgs lib;
#     manifests = kubenixCI2.manifestYAMLFile;
#   };
#
# Run locally:
#   nix build --file . nixosTests.containerd
#   # or: nix build --file . nixosTests.containerd.driverInteractive
#   # then: ./result/bin/nixos-test-driver (for interactive debugging)
#
# Requirements:
#   - KVM-capable host (or nested virt)
#   - Internet access for image pulls and nix binary cache

{
  pkgs,
  lib,
  manifests,
}:

pkgs.testers.nixosTest {
  name = "nixkube-containerd";

  nodes.control =
    { config, pkgs, ... }:
    {
      imports = [ ./cluster-module.nix ];

      # Make the manifest YAML available inside the VM
      virtualisation.additionalPaths = [ manifests ];

      # Forward host:2222 → VM:22 for interactive SSH debugging
      virtualisation.forwardPorts = [
        {
          from = "host";
          host.port = 2222;
          guest.port = 22;
        }
      ];
    };

  # Skip lint checks that fail on test environments
  skipLint = true;

  testScript = # python
    ''
      import json

      control.start()

      # Debug: Check if networking is available
      with control.nested("check networking"):
          control.wait_for_unit("network-online.target", timeout=90)

      control.wait_for_unit("containerd.service")

      # ── Phase 1: Bootstrap kubeadm cluster ──────────────────────────────

      with control.nested("kubeadm init"):
          control.succeed(
              "kubeadm init"
              " --pod-network-cidr=10.113.37.0/24"
              " --service-cidr=10.113.38.0/24"
              " --node-name=control"
              " --v=5"
              " 2>&1 | tee /tmp/kubeadm-init.log",
              timeout=300,
          )

      control.succeed("mkdir -p /root/.kube && cp /etc/kubernetes/admin.conf /root/.kube/config")

      # Remove control-plane taint so all pods (including test workloads) can schedule
      control.succeed(
          "kubectl taint nodes control node-role.kubernetes.io/control-plane:NoSchedule-"
      )

      # Wait for node to reach Ready (CNI + kubelet + kube-proxy must all converge)
      control.wait_until_succeeds(
          "kubectl wait --for=condition=Ready node/control --timeout=60s",
      )

      # Wait for CoreDNS to be available (needed for pod DNS resolution)
      control.wait_until_succeeds(
          "kubectl -n kube-system wait --for=condition=Available deployment/coredns --timeout=60s",
      )

      # Wait for kube-proxy to be ready (needed for Service ClusterIP routing)
      control.wait_until_succeeds(
          "kubectl -n kube-system wait --for=condition=Ready"
          " pod -l k8s-app=kube-proxy"
          " --timeout=60s",
      )

      # ── Phase 2: Deploy nixkube manifests ───────────────────────────────

      with control.nested("deploy nixkube"):
          control.succeed(
              "kubectl apply --server-side -f ${manifests} 2>&1 | tee /tmp/kubectl-apply.log"
          )

      # Wait for the init Job to create SSH secrets
      control.wait_until_succeeds(
          "kubectl -n nixkube get secret ssh-key",
          timeout=120,
      )

      # Wait for the DaemonSet node pod to be ready
      control.wait_until_succeeds(
          "kubectl -n nixkube wait --for=condition=Ready"
          " pod -l app.kubernetes.io/component=node"
          " --timeout=5s",
          timeout=600,
      )

      # Verify CSI driver is registered
      control.wait_until_succeeds(
          "kubectl get csidriver nixkube",
          timeout=30,
      )

      # ── Phase 3: Wait for test workloads ────────────────────────────────

      # CSI test Jobs are included in the manifest (from kubenix/ci/).
      # They have backoffLimit=6, so they'll retry if the CSI driver isn't
      # ready immediately. Wait for at least one CSI test to complete.

      with control.nested("wait for CSI test jobs"):
          # path-hello is a simple storePath CSI test
          control.wait_until_succeeds(
              "kubectl -n nixkube wait --for=condition=Complete"
              " job/path-hello"
              " --timeout=5s",
              timeout=600,
          )

      with control.nested("wait for NRI test jobs"):
          # nri-hello-ro tests NRI read-only mount
          control.wait_until_succeeds(
              "kubectl -n nixkube wait --for=condition=Complete"
              " job/nri-hello-ro"
              " --timeout=5s",
              timeout=600,
          )

      # ── Phase 4: Verify ────────────────────────────────────────────────

      with control.nested("verify results"):
          # Check that the CSI test pod ran successfully
          result = control.succeed(
              "kubectl -n nixkube get job path-hello -o jsonpath='{.status.succeeded}'"
          )
          assert result.strip().strip("'") == "1", f"path-hello job did not succeed: {result}"

          # Check that the NRI test pod ran successfully
          result = control.succeed(
              "kubectl -n nixkube get job nri-hello-ro -o jsonpath='{.status.succeeded}'"
          )
          assert result.strip().strip("'") == "1", f"nri-hello-ro job did not succeed: {result}"

          # Verify DaemonSet is healthy
          result = control.succeed(
              "kubectl -n nixkube get daemonset nix-node"
              " -o jsonpath='{.status.numberReady}'"
          )
          assert result.strip().strip("'") == "1", f"DaemonSet not ready: {result}"

          # Check for NixVolumeMount events (CSI driver reports these)
          events = control.succeed(
              "kubectl -n nixkube get events --field-selector reason=NixVolumeMount -o json"
          )
          event_data = json.loads(events)
          assert len(event_data.get("items", [])) >= 1, (
              f"Expected NixVolumeMount events, got {len(event_data.get('items', []))}"
          )

      # ── Dump state on success (useful for debugging) ───────────────────

      control.succeed("kubectl get nodes -o wide")
      control.succeed("kubectl -n nixkube get all")
      control.succeed("kubectl -n nixkube get events --sort-by=.lastTimestamp | tail -30")
    '';
}

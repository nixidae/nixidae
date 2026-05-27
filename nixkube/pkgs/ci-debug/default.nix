# SPDX-License-Identifier: MIT

{ lib, pkgs }:
let
  inherit (pkgs) writeShellApplication;
in
writeShellApplication {
  name = "ci-debug";
  runtimeInputs = [
    pkgs."claude-code"
    pkgs.kubectl
  ];
  text = # bash
    ''
      set -euo pipefail

      DS_API_KEY="''${DS_API:-''${ANTHROPIC_AUTH_TOKEN:-}}"
      if [ -z "$DS_API_KEY" ]; then
        echo "ci-debug requires DS_API or ANTHROPIC_AUTH_TOKEN to be set" >&2
        exit 1
      fi

      export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
      export ANTHROPIC_AUTH_TOKEN="$DS_API_KEY"
      export ANTHROPIC_MODEL="deepseek-v4-flash"
      export ANTHROPIC_DEFAULT_OPUS_MODEL="deepseek-v4-flash"
      export ANTHROPIC_DEFAULT_SONNET_MODEL="deepseek-v4-flash"
      export ANTHROPIC_DEFAULT_HAIKU_MODEL="deepseek-v4-flash"
      export CLAUDE_CODE_SUBAGENT_MODEL="deepseek-v4-flash"
      export CLAUDE_CODE_EFFORT_LEVEL="max"

      echo "=== ci-debug: Starting claude-code agentic diagnosis ===" >&2
      claude \
        --print \
        --dangerously-skip-permissions \
        "You are a Kubernetes SRE diagnosing a CI failure on a GitHub Actions runner.

      A Kind cluster (single-node, ephemeral) is running the nixkube CSI driver and NRI plugin.
      The CI pipeline just deployed nixkube to Kind and ran test workloads.

      CRITICAL RULE: Always verify claims with direct evidence. If an event says
      a resource was 'not found' at some point, check whether it EXISTS RIGHT NOW
      with \`kubectl get\`. Do not assume past events reflect current state.

      Investigation methodology:
      1. \`kubectl get pods -A -o wide\` — current pod states
      2. \`kubectl describe pod -n nixkube -l app.kubernetes.io/component=node\` — node pod, especially Init Containers section (exit codes, reasons)
      3. \`kubectl logs -n nixkube -l app.kubernetes.io/component=node -c initcopy --tail=100 --timestamps --previous\` — init container crash logs
      4. \`kubectl logs -n nixkube -l app.kubernetes.io/component=node --tail=50 --timestamps\` — node main container logs
      5. \`kubectl get events -n nixkube --sort-by='.lastTimestamp'\` — all events
      6. Verify each resource referenced in error events: \`kubectl get configmap -n nixkube\`, \`kubectl get secret -n nixkube\`, \`kubectl get serviceaccount -n nixkube\`
      7. \`kubectl logs -n nixkube -l job-name=init --tail=100\` — init job logs
      8. \`kubectl get csinode -o wide\` — CSI registration
      9. \`kubectl get volumeattachment -o wide\` — volume attachments

      Output your diagnosis in this exact format at the end. Every claim in
      Evidence must cite a specific command output that proves it:

      ## Root Cause
      <one line>

      ## Evidence
      - \`kubectl ...\` — <what it showed and why it matters>

      ## Fix
      <what to change to fix it>

      Do NOT modify cluster resources — this is read-only." || true

      echo "=== Fallback: static debug dump ==="
      kubectl get pods -A -o wide || true
      kubectl get pods -o wide -n nixkube || true
      echo "--- node pod describe ---"
      kubectl describe pod -l app.kubernetes.io/component=node -n nixkube || true
      echo "--- resources ---"
      kubectl get configmap,secret,serviceaccount -n nixkube 2>/dev/null || true
      echo "--- events ---"
      kubectl get events -n nixkube --sort-by='.lastTimestamp' || true
    '';
}

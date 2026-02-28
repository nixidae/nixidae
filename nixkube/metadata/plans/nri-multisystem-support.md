# NRI Multi-System Support Plan

## Overview

Add support for system-specific store path annotations in nix-nri. This allows mounting different store paths for different architectures (x86_64-linux, aarch64-linux, etc.) with the same destination path, activating only the one matching the current system.

## Annotation Format

Extend annotation keys with optional `@{system}` suffix:

```
nix-nri/{target}(-{suffix})?(@{system})?
```

- **target**: `pod` (wildcard) or `{container-name}` (container-specific)
- **suffix**: optional, allows multiple mounts to same destination (ignored by parser)
- **system**: optional, filters annotation to specific system

## Examples

### System-specific store paths
```nix
nix-nri/pod@x86_64-linux: /etc/myapp=/nix/store/x86-hash-myapp
nix-nri/pod@aarch64-linux: /etc/myapp=/nix/store/aarch64-hash-myapp
```

### With suffixes for multiple mounts
```nix
nix-nri/pod-ssl@x86_64-linux: /etc/ssl=/nix/store/cacert-x86-hash
nix-nri/pod-ssl@aarch64-linux: /etc/ssl=/nix/store/cacert-aarch64-hash

nix-nri/pod-app@x86_64-linux: /opt/app=/nix/store/app-x86-hash
nix-nri/pod-app@aarch64-linux: /opt/app=/nix/store/app-aarch64-hash
```

### All systems (backwards compatible)
```nix
nix-nri/pod: /etc/result=builtins.toFile "test" "value"
nix-nri/pod-suffix: /etc/ssl=/nix/store/cacert-hash
```

## Source Type Auto-Detection

Keep existing auto-detect logic for source values:
- Starts with `/nix/store/` → store path
- Contains `#` and looks like flake syntax → flake reference
- Otherwise → nix expression

**For store paths without system specified**: Assume they apply to current system (or all systems - TBD based on use case).

**For flakes**: System is resolved by `nix build`, no special handling needed.

## Implementation Changes

### 1. Update annotation parsing in `nriplugin.py`

- Modify `_parse_store_mounts_for_name()` to:
  - Parse `@{system}` suffix from annotation key
  - Accept optional `current_system` parameter
  - Skip annotations with `@{system}` that don't match current system

- Modify `parse_store_mounts()` to:
  - Accept `current_system` parameter
  - Pass to `_parse_store_mounts_for_name()`

### 2. Update CreateContainer handler

- Get current system (from environment or kubenix configuration)
- Pass system to `parse_store_mounts()` and `parse_nix_rw()`
- Filter annotations before building/mounting

### 3. Update nix-nri annotation parsing regex

Current pattern: `nix-nri/{target}(-{suffix})?`

New pattern: `nix-nri/{target}(-{suffix})?(@{system})?`

Allow:
- `nix-nri/pod`
- `nix-nri/pod-1`
- `nix-nri/pod@x86_64-linux`
- `nix-nri/pod-1@x86_64-linux`
- `nix-nri/{container-name}@aarch64-linux`
- `nix-nri/{container-name}-ssl@x86_64-linux`

## Backwards Compatibility

- Annotations without `@{system}` suffix apply to all systems (or current system if ambiguous)
- Existing annotations continue to work unchanged
- Suffix behavior unchanged (still allows multiple mounts to same destination)

## Notes

- Don't support multiple systems in single annotation (just add separate annotations if needed)
- System information comes from kubenix or environment variable
- Override semantics: container-specific annotations still override wildcard annotations regardless of system
- If both system-specific and system-agnostic annotations target same path, system-specific takes precedence (or error? TBD)

<div align="center">

<img src="https://raw.githubusercontent.com/nixidae/.github/main/assets/nixidae.svg" alt="Nixidae" width="150">

# Nixidae

**Python and Nix, in both directions.**

</div>

Nixidae is the home of a set of related projects: Python libraries that drive
Nix, Nix tooling written in Python, and infrastructure that hands Nix store
paths to systems that were never designed to receive them.

They live in one repository, [nixidae/nixidae](https://github.com/nixidae/nixidae),
so a change that crosses two projects lands in one commit. Each project is also
exported to a repository of its own. They share a
premise — that Nix is worth talking to from a real programming language, and
that Python is a good one for the job.

## The name

*Pythonidae* is the taxonomic family containing every python. *Nixidae* is the
sister family: same constrictor grip, same refusal to let go of anything it has
got hold of, but it reproduces by pure function of its inputs and will not enter
a habitat unless the entire habitat is in its closure.

Non-venomous. Bites anyway if you pass `--impure`.

## Projects

| Project | What it is |
| --- | --- |
| [**nanopynix**](https://github.com/nixidae/nanopynix) | nanobind-based Python bindings for Nix. Nix runs in an isolated worker subprocess and is driven over gRPC, so several differently configured Nix instances can run in parallel from one process. Async API for stores, evaluation and builds. |
| [**pynix**](https://github.com/nixidae/nanopynix/tree/main/pynix) | A Nix CLI built on nanopynix — `build`, `eval`, `develop`, `repl`, `store`, `path-info`, `flake`, option search — plus a Nix language server that understands the NixOS module system. |
| [**pynixd**](https://github.com/nixidae/pynixd) | The Nix daemon protocol, in Python. Sits between Nix clients and remote builders over SSH: deduplicates concurrent builds, caches path queries locally, and schedules work across multiple backends. |
| [**nixkube**](https://github.com/nixidae/nixkube) | Mounts `/nix` into Kubernetes pods through a CSI driver and an NRI plugin. Store paths, flake refs and Nix expressions become pod volumes with pod lifetime. |
| [**easykubenix**](https://github.com/nixidae/easykubenix) | Kubernetes manifests from the NixOS module system, without generating Nix types for the whole Kubernetes API. Validation runs against an ephemeral `etcd` and `kube-apiserver`. |
| [**vivarium**](https://github.com/nixidae/vivarium) | NixOS integration tests without KVM. Guests run as User-Mode Linux, QEMU or container processes, so a test is a derivation that passes or fails. |
| [**huggorm**](https://github.com/nixidae/huggorm) | Nix C++ bindings for Python, generated from a declaration language written in Python. One declaration gives the C++ binding, the type stubs and the async API. |
| [**havsorm**](https://github.com/nixidae/havsorm) | Salt states written in Nix and evaluated by huggorm. |
| [**pydd**](https://github.com/nixidae/pydd) | Dynamic derivations planned in Python: one derivation per package, from a lock file, for Python, Rust and npm. |
| [**ghanix**](https://github.com/nixidae/ghanix) | GitHub Actions workflows written as NixOS modules. |

## How the pieces fit

```mermaid
flowchart TD
    nanopynix["nanopynix<br/><i>Nix, as a Python library</i>"]
    pynix["pynix<br/><i>CLI + language server</i>"]
    pynixd["pynixd<br/><i>daemon protocol proxy</i>"]
    ekn["easykubenix<br/><i>manifests from modules</i>"]
    nixkube["nixkube<br/><i>/nix inside pods</i>"]

    nanopynix --> pynix
    ekn --> pynix
    ekn --> nixkube
    pynixd -. "remote builds" .-> nixkube

    subgraph Python
        nanopynix
        pynix
        pynixd
    end
    subgraph Kubernetes
        ekn
        nixkube
    end
```

`nanopynix` is the foundation: everything that needs to evaluate or build
without shelling out to the `nix` binary goes through it. `pynix` is what that
looks like as a tool you actually type. `pynixd` speaks the daemon wire
protocol instead of the C API, so it can slot in wherever a `nix-daemon` would.
`easykubenix` and `nixkube` are the Kubernetes end: one produces the manifests,
the other makes the store available to the pods they describe.

## Where to start

- **Curious what Nix-from-Python looks like?** Read the
  [nanopynix quickstart](https://nixidae.github.io/nanopynix/).
- **Want a faster `nix repl` / better editor support?** Look at `pynix`.
- **Running Nix builds across machines?** Look at `pynixd`.
- **Running Nix in Kubernetes?** Look at `nixkube`.

## Status

Every project lives in [nixidae/nixidae](https://github.com/nixidae/nixidae).
The repositories above moved here from [@Lillecarl](https://github.com/Lillecarl);
GitHub keeps the old URLs working.

Everything here is pre-1.0 and moving. Issues and pull requests are welcome on
[nixidae/nixidae](https://github.com/nixidae/nixidae) — see [CONTRIBUTING.md](https://github.com/nixidae/.github/blob/main/CONTRIBUTING.md)
for how that works, and [SECURITY.md](https://github.com/nixidae/.github/blob/main/SECURITY.md)
if you have found something that should not be reported in public.

---

<sub>

"Python" and the Python logos are trademarks of the Python Software Foundation,
used with permission. Nixidae is not affiliated with the Python Software
Foundation.

The Nixidae mark also incorporates the NixOS logo, © NixOS contributors,
licensed [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
Nixidae is not affiliated with the NixOS Foundation.

</sub>

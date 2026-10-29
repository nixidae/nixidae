# vivarium

NixOS integration tests, in the spirit of `nixosTest`. You describe some
guests as NixOS modules and write Python that checks them. vivarium boots
the guests, runs your checks and tears everything down.

The default guest is a User-Mode Linux kernel, which runs as an ordinary
process. It needs no `/dev/kvm` and no root, so a test runs in the Nix
build sandbox. A guest can also be a QEMU or libkrun virtual machine, or
a container.

```{toctree}
:maxdepth: 2
:caption: Contents

getting-started
howto
reference
test-options
guest-options
```

```{toctree}
:maxdepth: 1
:caption: Design

design/runner
design/console-guests
```

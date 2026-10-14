# zmq-sdist

pyzmq, built from its sdist against the `zeromq` of nixpkgs.

The backend is scikit-build-core, which runs CMake and Ninja during the
build. uv locks no build dependency, so the `build` group puts
scikit-build-core and Cython in the lock.

The project names no build tool. The node takes them from the pyzmq of
nixpkgs (`lib/python/hints.nix`): the compiler, CMake, Ninja, `zeromq`,
and `libsodium`, which zeromq links. Without `zeromq`, pyzmq would
download and bundle its own libzmq, which the sandbox refuses.

`../check-binary.nix` checks the environment, and requires the extension
to link the `zeromq` of nixpkgs.

# zmq-sdist

pyzmq, built from its sdist against the `zeromq` of nixpkgs.

The backend is scikit-build-core, which runs CMake and Ninja during the
build. uv locks no build dependency, so the `build` group puts
scikit-build-core and Cython in the lock. `[tool.pydd.build-tools]` gives
the pyzmq node the compiler, CMake, Ninja, `pkg-config` and `zeromq`.
pyzmq would otherwise download and bundle its own libzmq, which the
sandbox refuses.

`../check-binary.nix` checks the environment, and requires the extension
to link the `zeromq` of nixpkgs.

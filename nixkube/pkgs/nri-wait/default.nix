# NRI wait OCI hook - waits for Nix builds via ZeroMQ
# Runs inside chroot(/var/lib/nix-csi) so standard glibc is fine
{
  lib,
  buildPythonApplication,
  hatchling,
  pyzmq,
}:

buildPythonApplication {
  pname = "nri-wait";
  version = "0.1.0";

  src = ./.;

  pyproject = true;

  build-system = [ hatchling ];
  dependencies = [ pyzmq ];

  meta = with lib; {
    description = "OCI hook that waits for NRI build completion";
    homepage = "https://github.com/lillecarl/nix-csi";
    license = licenses.mit;
    maintainers = with maintainers; [ lillecarl ];
    platforms = platforms.linux;
  };
}

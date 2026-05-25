# SPDX-License-Identifier: MIT

{
  buildPythonApplication,
  hatchling,
  pynixd,
  kr8s,
  dockerTools,
  asyncinotify,
  lib,
}:
let
  pyproject = fromTOML (builtins.readFile ./pyproject.toml);
  range = lib.range;
  nixbldPasswdLines = map (
    i:
    "nixbld${toString i}:x:${toString (30000 + i)}:30000:Nix build user ${toString i}:/var/empty:/bin/sh"
  ) (range 1 32);
  nixbldGroupLines = [
    "nixbld:x:30000:${lib.concatStringsSep "," (map (i: "nixbld${toString i}") (range 1 32))}"
  ];
  extraPasswdLines = [
    "nix:x:1000:1000:Nix worker user:/nix/var/nix-csi/root:/bin/sh"
    "sshd:x:993:992:SSH privilege separation user:/var/empty:/bin/sh"
  ]
  ++ nixbldPasswdLines;
  extraGroupLines = [
    "nix:x:1000:"
    "sshd:x:992:"
  ]
  ++ nixbldGroupLines;
  fakeNss = dockerTools.fakeNss.override {
    inherit extraPasswdLines extraGroupLines;
  };
in
buildPythonApplication {
  pname = pyproject.project.name;
  version = pyproject.project.version;
  src = lib.cleanSource ./.;
  pyproject = true;
  build-system = [ hatchling ];
  dependencies = [
    pynixd
    kr8s
    asyncinotify
  ];
  makeWrapperArgs = [
    "--set"
    "FAKE_NSS"
    fakeNss
    "--set"
    "CA_CERTS"
    dockerTools.caCertificates
  ];
  meta.mainProgram = "pynixd-nixkube";
  passthru.fakeNss = fakeNss;
}

/*
  QEMU for macOS guests on a macOS host: the `vmapple` machine under HVF.

  The source is the `nixidae` branch of nixidae/qemu: upstream master with
  orchard's vmapple work and vivarium's changes on top (the `hvc #0`
  rewrite, a headless mode, a userspace virtio-vsock device that
  migrates). nixidae/nixidae#1168.

  nixpkgs' QEMU builds a release tarball, which vendors the meson
  subprojects. A git tree has only their wrap files, and the sandbox has no
  network for meson to fetch them, so the git subprojects are fetched here
  at the wraps' revisions.

  A local checkout replaces the source through overrideAttrs, not an
  argument: callPackage fills an argument named `src` with nixpkgs' `src`
  alias, which throws.
*/
{
  lib,
  qemu,
  fetchFromGitHub,
  fetchgit,
}:
let
  src = fetchFromGitHub {
    owner = "nixidae";
    repo = "qemu";
    rev = "887da5a976af636ee7e65f1be4da4918ee891db7";
    hash = "sha256-YuToHNl8kRuV9kjqnKKZy3XeFhO/WoXrvP7rIFP3cAs=";
  };
  gitlab =
    name: rev: hash:
    fetchgit {
      url = "https://gitlab.com/qemu-project/${name}.git";
      inherit rev hash;
    };
  subprojects = {
    keycodemapdb =
      gitlab "keycodemapdb" "f5772a62ec52591ff6870b7e8ef32482371f22c6"
        "sha256-EQrnBAXQhllbVCHpOsgREzYGncMUPEIoWFGnjo+hrH4=";
    berkeley-softfloat-3 =
      gitlab "berkeley-softfloat-3" "b64af41c3276f97f0e181920400ee056b9c88037"
        "sha256-Yflpx+mjU8mD5biClNpdmon24EHg4aWBZszbOur5VEA=";
    berkeley-testfloat-3 =
      gitlab "berkeley-testfloat-3" "e7af9751d9f9fd3b47911f51a5cfd08af256a9ab"
        "sha256-inQAeYlmuiRtZm37xK9ypBltCJ+ycyvIeIYZK8a+RYU=";
  };
in
(qemu.override {
  hostCpuTargets = [ "aarch64-softmmu" ];
  enableDocs = false;
}).overrideAttrs
  (old: {
    pname = "qemu-vmapple";
    version = "11.1.50-unstable-2026-11-08";
    inherit src;

    # meson turns vhost-user off on hosts other than Linux. QEMU's macOS
    # support for it is there (memory-backend-shm, pipe notifiers);
    # vhost-user-fs-pci needs it to reach darwin-virtiofsd. vhost-net
    # includes Linux's <linux/vhost.h> unconditionally (hw/net/vhost_net.c).
    configureFlags = old.configureFlags ++ [
      "--enable-vhost-user"
      "--disable-vhost-net"
    ];

    # Upstream master has it.
    patches = lib.filter (
      p: !lib.hasSuffix "fix-tls-tests-without-tasn1.patch" (toString p)
    ) old.patches;

    # meson applies a wrap's patch_directory only when it downloads; a
    # subproject already in place is used as it is.
    postPatch = (old.postPatch or "") + ''
      ${lib.concatStrings (
        lib.mapAttrsToList (name: path: ''
          cp -r --no-preserve=mode ${path} subprojects/${name}
          if [ -d subprojects/packagefiles/${name} ]; then
            cp -r --no-preserve=mode subprojects/packagefiles/${name}/. subprojects/${name}/
          fi
        '') subprojects
      )}
    '';
  })

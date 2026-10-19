/**
  Other distributions' cloud images, for `vivarium.image`.

  An image floats. openSUSE deletes a numbered build when the next one
  lands, so a pinned URL breaks with no commit (nixidae/nixidae#19).
  Evaluation reads the checksum that openSUSE publishes beside the
  floating name, and the download stays a fixed-output derivation with
  that hash.

  Needs impure evaluation: `builtins.fetchurl` without a hash is refused
  under `--pure-eval`. A run that crosses a new openSUSE build fails the
  hash once, and the next evaluation picks the new one up.
*/
{ fetchurl }:
let
  floating =
    url:
    let
      sums = builtins.readFile (builtins.fetchurl "${url}.sha256");
      hex = builtins.head (builtins.match "([0-9a-f]{64}) .*" sums);
    in
    fetchurl {
      inherit url;
      hash = "sha256:${hex}";
    };
in
{
  /**
    openSUSE Leap 16.0, the community build of SLE 16. Root is XFS on the
    third partition.

    Masked under UML, each measured on this image:
    - `jeos-firstboot`, an interactive wizard that loops on the console.
    - `kbdsettings`: UML has no keyboard, and `setleds` fails the unit.
  */
  opensuse-leap-16_0 = {
    disk = floating "https://download.opensuse.org/distribution/leap/16.0/appliances/Leap-16.0-Minimal-VM.x86_64-Cloud.qcow2";
    partition = 3;
    kernelParams = [
      "systemd.mask=jeos-firstboot.service"
      "systemd.mask=kbdsettings.service"
    ];
  };
}

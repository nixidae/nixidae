# And does kubeadm accept the configuration we generate for it?
{
  pkgs,
  lib,
  k8sConfig,
  ...
}:
pkgs.runCommand "kubeadm-config-valid" { nativeBuildInputs = [ pkgs.kubernetes ]; } (
  lib.concatMapStrings (file: ''
    echo "validating ${file}"
    kubeadm config validate --config ${file}
  '') (lib.attrValues k8sConfig.system.build.kubeadmConfigs)
  + "touch $out"
)

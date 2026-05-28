# SPDX-License-Identifier: MIT

{
  config,
  curPkgs,
  lib,
  subPath,
  inputs,
  ...
}:
let
  cfg = config.nixkube;
  system = curPkgs.stdenv.hostPlatform.system;

  containers = lib.mkNamedList {
    hello = {
      image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";
      command = [ "hello" ];
      volumeMounts = lib.mkNamedList {
        nixkube = {
          mountPath = "/nix";
          subPath = "nix";
        };
      };
    };
  };
in
{
  imports = [ ./nri.nix ];

  config = {
    nixkube.loggingConfig = {
      renderer = "json";
      loggers = {
        nixkube.level = "DEBUG";
        httpx.level = "WARNING";
      };
      root.level = "INFO";
    };
    # Shared substituters for all CI variants (Kind and NixOS test VM).
    # NixOS test VM has nix-serve at 10.113.37.1:5000 (PTP CNI gateway).
    # Nix handles dead/unreachable substituters gracefully so this is safe on Kind.
    nixkube.node.nixConfig.settings.substituters = [
      "https://nix-csi.cachix.org"
      "https://cache.nixos.org"
      "http://10.113.37.1:5000?trusted=1"
    ];
    kubernetes.resources.${cfg.namespace} = {
      ConfigMap.inputs = {
        metadata.labels = cfg.labels;
        data = inputs;
      };
      Job.flake-hello = {
        metadata.labels = cfg.labels // {
          "app.kubernetes.io/component" = "ci-test";
        };
        spec = {
          template = {
            spec = {
              restartPolicy = "Never";
              inherit containers;
              volumes = lib.mkNamedList {
                nixkube.csi = {
                  driver = "nixkube";
                  volumeAttributes.flakeRef = "github:nixos/nixpkgs/nixos-unstable#hello";
                };
              };
            };
          };
        };
      };
      Job.expr-hello = {
        metadata.labels = cfg.labels // {
          "app.kubernetes.io/component" = "ci-test";
        };
        spec = {
          template = {
            spec = {
              restartPolicy = "Never";
              containers = lib.mkNamedList {
                hello = {
                  image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";
                  command = [ "hello-unfree" ];
                  volumeMounts = lib.mkNamedList {
                    nixkube = {
                      mountPath = "/nix";
                      subPath = "nix";
                    };
                  };
                };
              };
              volumes = lib.mkNamedList {
                nixkube.csi = {
                  driver = "nixkube";
                  readOnly = true;
                  volumeAttributes.nixExpr = # nix
                    ''
                      let
                        nixpkgs = builtins.fetchTree {
                          type = "github";
                          owner = "nixos";
                          repo = "nixpkgs";
                          ref = "nixos-unstable";
                        };
                        pkgs = import nixpkgs { config = { allowUnfree = true; }; };
                      in
                      pkgs.hello-unfree # test that building works
                    '';
                };
              };
            };
          };
        };
      };
      Job.path-hello = {
        metadata.labels = cfg.labels // {
          "app.kubernetes.io/component" = "ci-test";
        };
        spec = {
          template = {
            spec = {
              restartPolicy = "Never";
              inherit containers;
              volumes = lib.mkNamedList {
                nixkube.csi = {
                  driver = "nixkube";
                  readOnly = true;
                  volumeAttributes.${system} = curPkgs.hello;
                };
              };
            };
          };
        };
      };
      Job.commandpath-hello = {
        metadata.labels = cfg.labels // {
          "app.kubernetes.io/component" = "ci-test";
        };
        spec = {
          template = {
            spec = {
              restartPolicy = "Never";
              containers = lib.recursiveUpdate containers {
                hello.command = [ (lib.getExe curPkgs.hello) ];
              };
              volumes = lib.mkNamedList {
                nixkube.csi = {
                  driver = "nixkube";
                  readOnly = true;
                };
              };
            };
          };
        };
      };
      Job.env-ssl = {
        metadata.labels = cfg.labels // {
          "app.kubernetes.io/component" = "ci-test";
        };
        spec = {
          template = {
            spec = {
              restartPolicy = "Never";
              containers = lib.recursiveUpdate containers {
                hello = {
                  command = [
                    (lib.getExe (
                      curPkgs.writeShellApplication {
                        name = "printer";
                        runtimeInputs = [
                          curPkgs.coreutils
                          curPkgs.hello
                        ];
                        text = # bash
                          ''
                            set -x
                            hello
                            ls -lah "$SSL_CERT_DIR"
                          '';
                      }
                    ))
                  ];
                  env = lib.mkNamedList {
                    SSL_CERT_FILE.value = "${curPkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
                    SSL_CERT_DIR.value = "${curPkgs.cacert}/etc/ssl/certs";
                  };
                  volumeMounts = lib.mkForce [
                    {
                      name = "nixkube";
                      mountPath = "/nix";
                      subPath = "nix";
                    }
                  ];
                };
              };
              volumes = lib.mkNamedList {
                nixkube.csi = {
                  driver = "nixkube";
                  readOnly = true;
                };
              };
            };
          };
        };
      };
      # Test failure scenarios for event reporting
      Job.invalid-storepath-hello = {
        metadata.labels = cfg.labels // {
          "app.kubernetes.io/component" = "ci-test";
        };
        spec = {
          template = {
            spec = {
              restartPolicy = "Never";
              inherit containers;
              volumes = lib.mkNamedList {
                nixkube.csi = {
                  driver = "nixkube";
                  readOnly = true;
                  volumeAttributes.${system} = "/nix/store/0000000000000000000000000000000-nonexistent";
                };
              };
            };
          };
        };
      };
      Job.invalid-flake-hello = {
        metadata.labels = cfg.labels // {
          "app.kubernetes.io/component" = "ci-test";
        };
        spec = {
          template = {
            spec = {
              restartPolicy = "Never";
              inherit containers;
              volumes = lib.mkNamedList {
                nixkube.csi = {
                  driver = "nixkube";
                  volumeAttributes.flakeRef = "github:nonexistent/nonexistent-repo/nonexistent-ref#nonexistent";
                };
              };
            };
          };
        };
      };
      Job.invalid-expr-hello = {
        metadata.labels = cfg.labels // {
          "app.kubernetes.io/component" = "ci-test";
        };
        spec = {
          template = {
            spec = {
              restartPolicy = "Never";
              inherit containers;
              volumes = lib.mkNamedList {
                nixkube.csi = {
                  driver = "nixkube";
                  volumeAttributes.nixExpr = # nix
                    ''
                      let
                        broken = this_identifier_does_not_exist;
                      in
                      broken
                    '';
                };
              };
            };
          };
        };
      };
    };
  };
}

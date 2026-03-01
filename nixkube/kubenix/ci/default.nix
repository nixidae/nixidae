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
  cfg = config.nix-csi;
  system = curPkgs.stdenv.hostPlatform.system;

  containers = lib.mkNamedList {
    hello = {
      image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";
      command = [ "hello" ];
      volumeMounts = lib.mkNamedList {
        nix-csi = {
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
      version = 1;
      formatters = {
        standard = {
          format = "%(levelname)s [%(name)s] %(message)s";
        };
      };
      handlers = {
        console = {
          class = "logging.StreamHandler";
          formatter = "standard";
          stream = "ext://sys.stdout";
        };
      };
      loggers = {
        nixkube = {
          level = "DEBUG";
          handlers = [ "console" ];
          propagate = false;
        };
        "nixkube.csi" = {
          level = "DEBUG";
          handlers = [ "console" ];
          propagate = false;
        };
        "nixkube.nri" = {
          level = "DEBUG";
          handlers = [ "console" ];
          propagate = false;
        };
        httpx = {
          level = "WARNING";
          handlers = [ "console" ];
          propagate = false;
        };
      };
      root = {
        level = "INFO";
        handlers = [ "console" ];
      };
    };
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
                    nix-csi = {
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
                        pkgs = import nixpkgs { };
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
                            ls -lah /etc/ssl/certs
                          '';
                      }
                    ))
                  ];
                  env = lib.mkNamedList {
                    SSL_CERT_FILE.value = "${curPkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
                    SSL_CERT_DIR.value = "${curPkgs.cacert}/etc/ssl/certs";
                  };
                  volumeMounts =
                    let
                      mkMount =
                        sPath:
                        let
                          extractSuffix =
                            sPath:
                            let
                              str = toString sPath;
                              matched = builtins.match "/nix/store/[0-9a-df-np-sv-z]{32}-[^/]+(.*)" str;
                            in
                            if matched == null then null else builtins.head matched;
                        in
                        {
                          name = "nix-csi";
                          mountPath = extractSuffix sPath;
                          subPath = subPath "${if lib.pathExists sPath then sPath else throw "path no good homes"}";
                          readOnly = true;
                        };
                      testEnv = curPkgs.buildEnv {
                        name = "testenv";
                        paths = [
                          curPkgs.dockerTools.fakeNss
                          curPkgs.dockerTools.binSh
                        ];
                      };
                    in
                    lib.mkForce [
                      (mkMount "${testEnv}/etc")
                      {
                        name = "nix-csi";
                        mountPath = "/nix";
                        subPath = "nix";
                        readOnly = true;
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

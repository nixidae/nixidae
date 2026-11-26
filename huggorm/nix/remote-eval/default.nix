/**
  Remote evaluation across two machines (nixidae/nixidae#738).

  `server` runs `huggorm.server --stdio --experimental-callbacks` under
  sshd. `client` holds a project tree on its own disk, opens SSH to the
  server and mounts the tree with `EvalState.mount`. Every file the
  evaluator reads crosses the connection as a callback.

  The tree is in the client's store view only, so a read that escapes to
  the server's disk fails instead of passing by accident.

  QEMU by default: the UML kernel has no `netem`, which the latency
  sweep needs, and UML timings mislead.

      nix run --file . internal.remote-eval.driver -- --out DIR
*/
{
  lib,
  pkgs,
  vivarium,
  nixvenv,
  python3,
  huggorm,
  huggorm-bindings,
  huggorm-generated,
}:
let
  venv = nixvenv {
    python = python3;
    packages =
      _:
      [
        huggorm-bindings
        huggorm-generated
        huggorm
      ]
      ++ huggorm.optional-dependencies.ssh;
    executables = [ "python3" ];
  };

  python = "${venv}/bin/python3";
  socket = "/run/huggorm/eval.sock";
  listenPort = 2222;

  lan = address: {
    vivarium.lan = {
      network = "remote-eval";
      inherit address;
    };
  };
in
vivarium.mkTest {
  name = "huggorm-remote-eval";
  backend = "qemu";
  phases = {
    test = {
      script = ./test.py;
      after = [ "boot" ];
    };
    warm = {
      script = ./warm-test.py;
      after = [ "test" ];
    };
    latency = {
      script = ./latency-test.py;
      after = [ "warm" ];
    };
  };
  nodes = {
    server = {
      imports = [ (lan "192.168.98.2/24") ];
      # vivarium's own sshd port is 4326; the client dials the usual one.
      services.openssh = {
        enable = true;
        ports = [ 22 ];
        # sshd ends a session whose client stops answering, so the warm
        # server sees its connection close: 3 × 2 s.
        settings = {
          ClientAliveInterval = 2;
          ClientAliveCountMax = 3;
        };
      };
      # The warm server: one process that outlives every connection.
      systemd.services.huggorm-server = {
        wantedBy = [ "multi-user.target" ];
        serviceConfig = {
          # Also on SSH itself, beside sshd: the client's key reaches
          # root's authorized_keys after boot, and the listener reads the
          # file at each connection.
          ExecStart = lib.concatStringsSep " " [
            "${python} -c 'from huggorm.server import main; main()' ${socket}"
            "--experimental-callbacks"
            "--ssh-listen 0.0.0.0:${toString listenPort}"
            "--ssh-host-key /var/lib/huggorm/host_key"
            "--ssh-authorized-keys /root/.ssh/authorized_keys"
          ];
          RuntimeDirectory = "huggorm";
          StateDirectory = "huggorm";
        };
      };
      networking.firewall.allowedTCPPorts = [ listenPort ];
      # git: a `git` flake input fetches with the git binary.
      environment.systemPackages = [
        venv
        pkgs.git
      ];
      # A project with flake inputs fetches them here, into the guest's
      # store, which lives in memory. Guest RAM is sparse, so the ceiling
      # costs nothing until a fetch touches it.
      vivarium.memory = "6144M";
      nix.settings.experimental-features = [
        "nix-command"
        "flakes"
      ];
    };
    client = {
      imports = [ (lan "192.168.98.3/24") ];
      environment.systemPackages = [ venv ];
      vivarium.lateClosure = [
        "${./client.py}"
        "${./warm.py}"
        "${./project}"
      ];
    };
  };
  settings = {
    inherit python socket listenPort;
    client = "${./client.py}";
    warm = "${./warm.py}";
    project = "${./project}";
    server = "192.168.98.2";
  };
}

/**
  Remote evaluation across two machines (nixidae/nixidae#738).

  `server` runs `huggorm.server --stdio --experimental-callbacks` under
  sshd. `client` holds a project tree on its own disk, opens SSH to the
  server and mounts the tree with `EvalState.mount`. Every file the
  evaluator reads crosses the connection as a callback.

  The tree is in the client's store view only, so a read that escapes to
  the server's disk fails instead of passing by accident.

      nix run --file . internal.remote-eval.qemu.driver -- --out DIR
*/
{
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

  lan = address: {
    vivarium.lan = {
      network = "remote-eval";
      inherit address;
    };
  };
in
vivarium.mkTest {
  name = "huggorm-remote-eval";
  phases.test = {
    script = ./test.py;
    after = [ "boot" ];
  };
  nodes = {
    server = {
      imports = [ (lan "192.168.98.2/24") ];
      # vivarium's own sshd port is 4326; the client dials the usual one.
      services.openssh = {
        enable = true;
        ports = [ 22 ];
      };
      environment.systemPackages = [ venv ];
    };
    client = {
      imports = [ (lan "192.168.98.3/24") ];
      environment.systemPackages = [ venv ];
      vivarium.lateClosure = [
        "${./client.py}"
        "${./project}"
      ];
    };
  };
  settings = {
    inherit python;
    client = "${./client.py}";
    project = "${./project}";
    server = "192.168.98.2";
  };
}

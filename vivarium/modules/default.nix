# Makes a NixOS configuration bootable under User-Mode Linux.
#
# UML compiles the kernel as an ordinary Linux program, so a "VM" here is
# just a process: no KVM, no root, no tap devices.  The pieces are split
# across three files:
#
#   default.nix  the vivarium options and the packages a guest needs
#   guest.nix    what the guest system itself looks like
#   image.nix    the root image, /init, and the vivarium-run wrapper
{
  config,
  lib,
  pkgs,
  extendModules,
  ...
}:
let
  tools = import ./tools.nix { inherit pkgs lib; };

  portPair = lib.types.submodule {
    options = {
      host = lib.mkOption {
        type = lib.types.nullOr lib.types.port;
        default = null;
        description = ''
          Port to listen on, on the host. `null`: the runner picks the
          guest's port, or the first free port above it, when the run
          starts. `<out>/access.json` and `Machine.reachable` name it.
        '';
      };
      guest = lib.mkOption {
        type = lib.types.port;
        description = "Port it reaches inside the guest.";
      };
    };
  };

  # A host port and the guest port behind it, written as a bare port
  # when the two are the same -- which they are unless the host will not
  # give us the number the guest wants.
  samePort = port: {
    host = port;
    guest = port;
  };
  portMap = lib.types.coercedTo lib.types.port samePort portPair;

  forwardRule = lib.types.submodule {
    options = {
      address = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "0.0.0.0";
        description = ''
          Host address to listen on.  Null means the runner picks this
          guest a free address out of `127.0.0.2` upwards and keeps it
          for the guest's lifetime, which is what makes two guests able
          to serve the same port number without arranging anything.

          Anything else is taken literally, so `0.0.0.0` reaches the
          guest from off the machine.  Note that a shared address
          collides with every other guest's `all` rule, one port at a
          time, so give it an explicit `ports` list.
        '';
      };

      ports = lib.mkOption {
        type = lib.types.either (lib.types.enum [ "all" ]) (lib.types.listOf portMap);
        default = "all";
        example = lib.literalExpression "[ 8080 { host = 9090; guest = 80; } ]";
        description = ''
          Which ports to forward, or `"all"` for every port passt is
          willing to bind on this address.

          `"all"` costs about 36000 sockets and 17 MB, and takes under a
          second, which is cheap enough that a guest with an address to
          itself need not know its own port list in advance.  It is
          still not free: a test that boots three guests does not want
          it, which is why the default here is the ssh port alone.
        '';
      };

      protocols = lib.mkOption {
        type = lib.types.listOf (
          lib.types.enum [
            "tcp"
            "udp"
          ]
        );
        default = [ "tcp" ];
        description = ''
          Which protocols to forward these ports for.  UDP doubles the
          socket count, so it is off unless asked for.
        '';
      };

      remapPrivileged = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = ''
          What to do about host ports below
          `net.ipv4.ip_unprivileged_port_start`, which nothing here may
          bind: move them up by `privilegedOffset`, so guest port 22 is
          reachable on host port 10022.

          The runner says so on the console every time it does this --
          a port that is not the port you asked for is worth hearing
          about at boot rather than deducing from a refused connection.
          Turn this off to leave those ports unforwarded instead.
        '';
      };

      privilegedOffset = lib.mkOption {
        type = lib.types.port;
        default = 10000;
        description = ''
          How far up to move privileged ports.  The default keeps the
          original port readable in the new one: 22 becomes 10022, 80
          becomes 10080.
        '';
      };
    };
  };

  interfaceType = lib.types.submodule {
    options = {
      segment = lib.mkOption {
        type = lib.types.str;
        example = "fabric-a";
        description = ''
          The Ethernet segment this interface is plugged into.  Every
          interface in a run naming the same segment is on one link.
        '';
      };
      addresses = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = [
          "10.0.0.1/31"
          "fd00:a::1/64"
        ];
        description = ''
          Static addresses with their prefix length, IPv4 or IPv6.  Empty
          leaves the interface with its IPv6 link-local address alone.
        '';
      };
    };
  };

  hex = n: (lib.optionalString (n < 16) "0") + lib.toHexString n;

  # `vec1` first, so the `lan` shorthand keeps the NIC and MAC it always
  # had; the rest by name. UML names NIC n `vecN` before the guest renames
  # it, which is why no other `vecN` may be a name.
  nicsOf =
    interfaces: index: mtu: segments:
    let
      names = lib.attrNames interfaces;
      ordered = lib.optional (interfaces ? vec1) "vec1" ++ lib.filter (name: name != "vec1") names;
    in
    lib.imap1 (
      nic: name:
      let
        inherit (interfaces.${name}) segment addresses;
        own = segments.${segment}.mtu or null;
      in
      {
        inherit
          name
          nic
          segment
          addresses
          ;
        mtu = if own != null then own else mtu;
        mac = "52:54:00:12:${hex nic}:${hex index}";
      }
    ) ordered;
in
{
  # The guest kinds (appliance, image, routeros, sonic, vyos) are not here:
  # a guest imports the one it is, from `nixosModules`.
  imports = [
    ./container.nix
    ./guest.nix
    ./image.nix
    ./nftables.nix
    ./qemu.nix
    ./store.nix
  ]
  ++
    map
      (
        option:
        lib.mkRemovedOptionModule [ "vivarium" "typeCheck" option ] (
          if option == "extraPackages" then
            "Set the run's `python.packages` in mkTest."
          else
            "Set the run's `typeCheck.${option}` in mkTest."
        )
      )
      [
        "enable"
        "extraPackages"
        "strict"
        "ignore"
      ];

  options.vivarium = {
    backend = lib.mkOption {
      type = lib.types.enum [
        "uml"
        "qemu"
        "krun"
        "container"
      ];
      default = "uml";
      description = ''
        Which machine a guest becomes.

        `uml` is a process: no KVM, no root, no tap device, nothing asked
        of the host. `qemu` needs `/dev/kvm` to be worth running, and is
        then multiprocessor and much faster. `krun` is a libkrun microVM
        with the same kernel, initrd and disk as `qemu`. It serves the store
        through a DAX window, so a guest keeps no copy of the store pages it
        reads, but it has no monitor: no screen, and no `shrink` or `grow`.
        `container` runs the system
        under rootless crun on the host's own kernel: no kernel boot, but
        no kernel of its own either, and the host needs user namespaces,
        subordinate ids and a writable cgroup. Its uplink and forwards
        are pasta's, and its LAN shares a segment with the other two.

        The guest is the same NixOS configuration either way, and so is
        the test script. What changes is the kernel and how `/nix`
        arrives: UML builds its own kernel and mounts the store over
        hostfs, QEMU uses the host's kernel with an initrd and mounts it
        over virtiofs.
      '';
    };

    index = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 0;
      description = ''
        This guest's position in its test, set by `mkTest`.

        It is what keeps two guests on one segment apart: their MAC
        addresses carry it, and under QEMU the interface names are
        matched on those MACs.
      '';
    };

    agentDevice = lib.mkOption {
      type = lib.types.str;
      default = "/dev/ttyS0";
      internal = true;
      description = ''
        The device the in-guest agent serves on. The backend decides it,
        because the console has to go somewhere else.
      '';
    };

    memory = lib.mkOption {
      type = lib.types.either lib.types.str (
        lib.types.submodule {
          options.scale = lib.mkOption {
            type = lib.types.addCheck lib.types.number (x: x > 0) // {
              description = "positive number";
            };
            description = "The host's available memory times this, at least 512M.";
          };
        }
      );
      default =
        if config.vivarium.backend == "krun" then
          { scale = 1; }
        else if config.vivarium.backend == "qemu" then
          "512M"
        else
          "256M";
      defaultText = lib.literalExpression ''if backend is "krun" then { scale = 1; } else if backend is "qemu" then "512M" else "256M"'';
      example = "1024M";
      description = ''
        Guest RAM, as UML's `mem=` and QEMU's `-m` both take it, or
        `{ scale = s; }` for the host's `MemAvailable` times `s`, which the
        runner reads when the guest starts. A krun guest gets the scale by
        default: it backs only the pages it touches, and free page
        reporting gives freed pages back, so a high ceiling costs the host
        nothing until the guest uses it. Each guest reads the host on its
        own, so guests that start together can each get the whole host.
        UML's `vivarium-run` takes a size only.

        A ceiling rather than a cost, under UML. The guest's memory is
        a sparse file and the guest punches holes in it as it frees
        pages, so the host pays for what a guest is using rather than
        for what it has ever used. `vm.host_memory_kib()` is that
        number; see the README.

        Under UML, below about 192M the kernel starts OOM-killing the
        agent while systemd and Python are both resident.

        A QEMU guest needs more for the same work, which is why the
        default is not one number. It boots a stock NixOS kernel with its
        modules and a systemd initrd, where UML boots one kernel with
        everything built in. Measured: at 256M the agent reaches
        its ready line and is OOM-killed seconds later, inside the build
        sandbox, on a guest that passes the same test outside it.
      '';
    };

    reclaim = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = config.vivarium.backend == "krun";
        defaultText = lib.literalExpression ''backend == "krun"'';
        description = ''
          Reclaim memory the guest has not touched for `minAge`, with the
          kernel's DAMON_RECLAIM. QEMU and krun guests only.

          A guest keeps its page cache while it has free memory, and the
          host pays for every cached page. A free page costs the host
          nothing: the balloon reports it and the host takes it back. So
          a guest that drops cold cache gives its memory to the host by
          itself, with no balloon request.

          On by default under krun, which is for dense guests: its store
          reads cost the guest no cache, so what is left to reclaim is the
          guest's own disk and its processes' files.
        '';
      };
      minAge = lib.mkOption {
        type = lib.types.ints.positive;
        default = 120;
        example = 30;
        description = ''
          Seconds a region of memory goes unaccessed before it is
          reclaimed. DAMON_RECLAIM's own default.
        '';
      };
    };

    seccomp = lib.mkOption {
      type = lib.types.enum [
        "auto"
        "on"
        "off"
      ];
      default = "auto";
      example = "off";
      description = ''
        How UML catches a guest process's syscalls. Ignored by QEMU.

        `on` installs a seccomp filter and lets the guest's own signal
        handler do the memory management, which costs fewer context
        switches per minor fault -- a few percent of throughput and about
        five seconds of boot. `auto` uses it where the host allows the
        filter and falls back to ptrace where it does not. `off` is the
        ptrace userspace, which is a decade older and much better tested.

        UML's own `--help` says the filter "is not (yet) restrictive
        enough to prevent userspace from reading and writing all physical
        memory", so a guest process can corrupt the guest kernel's own
        structures.

        This is not the lever for a panic in the memory manager, whatever
        that suggests: issue #8 panics on both userspaces, and the cause
        was io_uring. See modules/guest.nix.
      '';
    };

    cpus = lib.mkOption {
      type = lib.types.either lib.types.ints.positive (
        lib.types.submodule {
          options.scale = lib.mkOption {
            type = lib.types.addCheck lib.types.number (x: x > 0) // {
              description = "positive number";
            };
            description = "The host's cores times this, rounded, at least one.";
          };
        }
      );
      default =
        if
          lib.elem config.vivarium.backend [
            "qemu"
            "krun"
          ]
        then
          { scale = 1; }
        else
          1;
      defaultText = lib.literalExpression ''if backend is "qemu" or "krun" then { scale = 1; } else 1'';
      example = lib.literalExpression "{ scale = 0.5; }";
      description = ''
        Processors the guest gets: a number, or `{ scale = s; }` for the
        host's cores times `s`. The runner counts the cores when the
        guest starts, so the derivation does not depend on the machine
        that evaluated it: `NIX_BUILD_CORES` inside a build, else the
        cores this process may run on. Each guest sets its own here.
        `vivarium run --cpus [GUEST=]N` or `--cpu-scale [GUEST=]S`
        replaces it for one run: one guest, or every guest not named. QEMU and krun honour
        it; UML and container guests do not.

        Worth about 11% on the iperf test at two processors, measured.
        Under UML two vCPUs measured *slower* than one on that same test
        -- UML allows SMP with the seccomp userspace alone, and the
        cross-CPU work costs more than the parallelism buys. The two
        backends disagree here, so do not carry a number between them.
      '';
    };

    nestedVirtualization = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Give the guest `/dev/kvm`, so it can run virtual machines of its
        own. QEMU, krun and container backends.

        A container guest shares the host's kernel, so it gets the host's
        own `/dev/kvm` as a bind mount, and needs no nesting.

        On QEMU, off, the runner hides `vmx` and `svm` from the guest's CPU. On, it
        passes the host's through, which needs nesting on the host
        (`/sys/module/kvm_{intel,amd}/parameters/nested`), and the guest
        loads KVM for that vendor. Without nesting on the host, the
        guest's `vivarium-kvm` unit fails with the reason.
      '';
    };

    efi = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Boot the guest through UEFI firmware and systemd-boot, as an
        installed machine boots. QEMU backend only.

        The guest gets OVMF, a variable store that lives as long as the
        run, and a second disk with an EFI system partition on `/boot`.
        The first boot loads the kernel directly, and a unit installs
        systemd-boot for the booted system before the agent comes up.
        Each later boot is the firmware's: systemd-boot picks the entry,
        so `boot.loader.systemd-boot` options such as `bootCounting` take
        effect.

        Off, the runner is the bootloader: after a reboot it boots the
        bootspec that the guest's last `switch-to-configuration` wrote.
      '';
    };

    display = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          Give the guest a screen, a keyboard and a pointer. QEMU backend
          only.

          The host reads the screen with QMP's `screendump` and types and
          points with QMP's input commands, so `Machine.screenshot`,
          `wait_for_text`, `send_chars` and `click` work whatever runs in
          the guest. A getty runs on tty1, so a guest with no compositor
          shows a login prompt.
        '';
      };
      width = lib.mkOption {
        type = lib.types.ints.positive;
        default = 1024;
        description = "Width of the screen in pixels.";
      };
      height = lib.mkOption {
        type = lib.types.ints.positive;
        default = 768;
        description = "Height of the screen in pixels.";
      };
    };

    diskSize = lib.mkOption {
      type = lib.types.ints.positive;
      default = 512;
      description = ''
        Size of the root image in MiB.  It holds almost nothing -- the
        Nix store comes from the host over hostfs -- so this only has to
        cover what the guest writes at runtime.
      '';
    };

    mtu = lib.mkOption {
      type = lib.types.ints.between 576 65534;
      default = 65000;
      example = 1500;
      description = ''
        MTU of both `vec` interfaces.

        A segment is a socketpair, and AF_UNIX only lets about ten
        frames sit in one before the sender blocks, so frame size is
        what decides how much a guest can have in flight: jumbo frames
        are worth roughly twice the throughput of 1500-byte ones.

        The default stops short of 64 KiB on purpose.  The driver keeps
        a receive buffer of `mtu` + 66 bytes per queue slot, and once
        that plus the skb's own footer passes 64 KiB each one costs a
        128 KiB allocation instead.  65520 measures the same as 65000
        and uses twice the memory to do it.

        Set this to 1500 for a test that cares about behaving like real
        Ethernet.  It cannot be changed from inside the guest: the
        driver leaves `max_mtu` at 1500, so this is the only way up.
      '';
    };

    journal = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Stream this guest's journal to `/artifacts/journal.jsonl` while
        it runs, one JSON object per entry.

        The host follows the file and turns each entry into an event
        that carries the machine and the unit. hostfs and virtiofs are
        both write-through, so an entry is on the host's disk as soon as
        journald has it -- a guest that is killed keeps everything it
        logged up to that moment.
      '';
    };

    sshPort = lib.mkOption {
      type = lib.types.port;
      default = 4325;
      description = ''
        Port sshd listens on, forwarded from the same port on the host's
        loopback by passt.  Tests drive the guest over the serial line
        instead; this is for looking around by hand.
      '';
    };

    rootPassword = lib.mkOption {
      type = lib.types.str;
      default = "uml";
      description = ''
        Root's password.  The guest is only reachable through the passt
        forward on the host's loopback, so this is a convenience rather
        than a secret -- do not put anything real in a guest.
      '';
    };

    forward = lib.mkOption {
      type = lib.types.listOf forwardRule;
      default = [ { ports = [ config.vivarium.sshPort ]; } ];
      defaultText = lib.literalExpression "[ { ports = [ config.vivarium.sshPort ]; } ]";
      example = lib.literalExpression ''
        [
          { ports = "all"; }                              # the whole guest, privately
          { address = "0.0.0.0"; ports = [ 8080 ]; }      # and one port, publicly
        ]
      '';
      description = ''
        How the host reaches services in this guest.

        passt is the only way in, and its forwards are fixed once it has
        started: it binds every socket while parsing its arguments, and
        has no way to be told about a new one afterwards short of a
        restart that would drop every connection through it.  So this is
        decided before the guest boots, and `ports = "all"` exists to
        make not having to decide affordable.
      '';
    };

    /*
      Tell Nix, inside the guest, about the store it can already see.

      `/nix/store` in a guest is the host's, over hostfs, with a writable
      overlay on top -- see image.nix.  Every path is there and readable,
      and Nix knows about none of them: there is no `/nix/var/nix/db` at
      all, so `nix-store --query --references <a path that is right there>`
      answers `path '...' is not valid`.

      That is fine for a guest that only runs programs.  It is not fine for
      one that runs Nix: a build or a `nix copy` into a second store finds
      its inputs invalid, tries to substitute them, and a build sandbox has
      no network.  So this builds a database for the closure below, which
      turns a directory the guest can see into a store it can use.

      On by default, because it stopped being worth deciding about.  It
      was off while it cost a oneshot at every boot; now it is built with
      the image, and measured at 612ms and 256K for a minimal guest and
      561ms and 268K for a kubeadm control plane, cached after the first
      build.  Against that, a guest without one looks identical to a guest
      with one until something runs Nix, and fails then as a network
      timeout that names nothing.
    */
    nixDatabase = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        example = false;
        description = ''
          Build a Nix database for the store the guest sees, and put it on
          the root image.

          Turning this off leaves a guest whose `/nix/store` is full of
          paths that Nix in there calls invalid.
        '';
      };

      loader = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        internal = true;
        default =
          if config.vivarium.nixDatabase.enable then "${config.nix.package.out}/bin/nix-store" else null;
        defaultText = lib.literalExpression ''"''${config.nix.package.out}/bin/nix-store"'';
        description = ''
          The `nix-store` the runner calls in the guest to register a path
          after boot (`add_closure`). `null` is a guest with no Nix
          database: the runner adds the path to its store view only.
        '';
      };

      extraRoots = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = lib.literalExpression ''[ "''${pkgs.hello}" ]'';
        description = ''
          Store paths to register beyond the guest's own system closure.

          Each one becomes a dependency of the root image, so an edit to
          one rebuilds it. A path a test changes often, such as a source
          tree, belongs in `vivarium.lateClosure`.
        '';
      };
    };

    lateClosure = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = lib.literalExpression ''[ "''${./tests}" ]'';
      description = ''
        Store paths the runner adds to the guest's store view, and
        registers in its Nix database, once the guest is up.

        `mkTest`'s `settings` are data: a path they name reaches a guest
        only through this list or `add_closure`. Unlike
        `nixDatabase.extraRoots`, these paths are not in the root image,
        so an edit to one rebuilds a closureInfo and the run's spec, not
        the guest.

        A guest with no Nix database (a cloud image without
        `writableStore`) sees the paths and does not register them.
      '';
    };

    requires = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "cache" ];
      description = ''
        Guests this one cannot boot without, by name in `nodes`. A run
        given `--only` boots only the guests its phases need, and these
        with them. A full run boots every guest anyway.

        The recipe that adds the dependency sets it: the `cache` recipe
        sets it on every other guest, which waits for the cache at boot.
      '';
    };

    configurations = lib.mkOption {
      type = lib.types.attrsOf lib.types.deferredModule;
      default = { };
      example = lib.literalExpression ''
        { pynixd = { services.pynixd.enable = true; }; }
      '';
      description = ''
        Configurations this guest can switch to while it runs, by name.

        Each one is this guest plus the module, through `extendModules`, so
        it keeps the hostname, the segment and the backend. Its system is in
        the guest's Nix database, and a phase switches with
        `await vm.switch_to("pynixd")`, or back with `vm.switch_to()`.

        Evaluated here, on the host: the guest has no nixpkgs and no
        network to build one with.
      '';
    };

    python.packages = lib.mkOption {
      type = lib.types.listOf lib.types.package;
      default = [ ];
      example = lib.literalExpression "[ pkgs.python3Packages.pyroute2 ]";
      description = ''
        Python packages, on the host, that a phase needs to drive this
        guest. They go into the run's venv beside the run's own
        `python.packages`, and the type check reads them too.

        The module that gives a guest its kind sets this, so a test that
        uses the guest needs no import list of its own.
      '';
    };

    launch = lib.mkOption {
      type = lib.types.nullOr (
        lib.types.submodule {
          options = {
            backend = lib.mkOption {
              type = lib.types.str;
              description = "The backend that reads `config`.";
            };
            config = lib.mkOption {
              type = lib.types.attrsOf lib.types.anything;
              default = { };
              description = ''
                What the backend needs to start this guest: its tool paths
                and parameters. The backend's Python class validates it and
                refuses a field it does not know.
              '';
            };
          };
        }
      );
      default = null;
      internal = true;
      description = ''
        How the backend starts this guest. The backend's own module writes
        it; a test does not.
      '';
    };

    control = {
      kind = lib.mkOption {
        type = lib.types.str;
        default = "agent";
        description = ''
          How the host drives this guest: `agent`, `none` (the console's
          `ready` line says the guest is up, and nothing drives it), or the
          name of a console dialect in the `vivarium.controls` entry
          points, such as `routeros`.
        '';
      };
      config = lib.mkOption {
        type = lib.types.attrsOf lib.types.anything;
        default = { };
        description = ''
          What the control kind needs. Its Python owner validates it and
          refuses a field it does not know.
        '';
      };
    };

    kind = lib.mkOption {
      type = lib.types.str;
      default = "nixos";
      internal = true;
      description = ''
        What runs in this guest: `nixos`, this evaluation's system, or the
        name of the guest kind whose module replaces it, such as
        `routeros`. Two kinds on one guest conflict here.
      '';
    };

    rootImage = lib.mkOption {
      type = lib.types.path;
      default = config.system.build.vivariumRootImage;
      defaultText = lib.literalExpression "config.system.build.vivariumRootImage";
      internal = true;
      description = "The disk the guest boots. A guest kind sets its own.";
    };

    closure = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = config.system.build.vivariumNixRegistration;
      defaultText = lib.literalExpression "config.system.build.vivariumNixRegistration";
      internal = true;
      description = ''
        A closureInfo of what the guest sees of the store, or null for a
        guest that sees none of it. The runner builds the store view from
        its `store-paths`.
      '';
    };

    requiredSystemFeatures = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      internal = true;
      description = ''
        What the Nix daemon must give the run's derivation for this guest,
        such as `kvm`. The backend's module adds to it.
      '';
    };

    interfaces = lib.mkOption {
      type = lib.types.attrsOf interfaceType;
      default = { };
      example = lib.literalExpression ''
        {
          # "uplink" is the interface name in the guest; "leaf1-spine1" is
          # the cable it is plugged into, shared with the peer's end.
          uplink = { segment = "leaf1-spine1"; };
          servers = { segment = "rack1"; addresses = [ "10.1.0.1/24" ]; };
        }
      '';
      description = ''
        The guest's links to Ethernet segments, by interface name: the
        attribute name is the interface's name inside the guest, on every
        backend.  So a test, or a router's configuration, names the link
        it means.  Each peer's address is in /etc/hosts as
        `<host>.<segment>`.

        `vec0` is the uplink and not one of these.  Names are at most 15
        characters, and no `vecN` but `vec1`, which `lan` defines.
      '';
    };

    uplink.segment = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "mgmt";
      description = ''
        The Ethernet segment the uplink (`vec0`) joins instead of passt:
        an out-of-band management network a peer serves with DHCP (see the
        `servicesNode` helper; nixidae/nixidae#958). The guest leases its
        management address there. It then has no passt uplink, and so no
        forwards. QEMU only.
      '';
    };

    uplink.proxy = lib.mkOption {
      type = lib.types.enum [
        "passt"
        "gvproxy"
      ];
      default = "passt";
      description = ''
        The host process that serves the uplink (`vec0`). `gvproxy` is
        gvisor-tap-vsock's, which runs on macOS too and needs no user
        namespace (nixidae/nixidae#1163). It gives the guest passt's
        addresses, and forwards only listed TCP ports: no `ports = "all"`
        and no UDP. krun only.
      '';
    };

    segments = lib.mkOption {
      internal = true;
      type = lib.types.attrsOf (import ./segment.nix lib);
      default = { };
      description = "The run's `segments`, which `mkTest` gives every guest.";
    };

    nics = lib.mkOption {
      internal = true;
      readOnly = true;
      description = "`interfaces` in NIC order, each with its NIC number, MAC and MTU.";
    };

    uplinkMac = lib.mkOption {
      internal = true;
      readOnly = true;
      type = lib.types.str;
      description = ''
        The uplink's (`vec0`) MAC, matching the runner's `spec.mac(0)`
        (lowercase). A peer that must name this guest by its uplink MAC --
        a DHCP reservation, a VyOS link rename -- reads it here rather than
        spelling the scheme out again.
      '';
    };

    lan = {
      network = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "lan";
        description = ''
          Shorthand for `interfaces.vec1.segment`.
          Name of an Ethernet segment to join on `vec1`.  Every machine
          in a test naming the same segment is wired together; the host
          runner creates the sockets.  Null leaves the guest with only
          its passt uplink.
        '';
      };

      address = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "192.168.99.2/24";
        description = ''
          Static address and prefix for `vec1`.  There is no DHCP server
          on a segment, so machines that need to talk to each other need
          one of these each.
        '';
      };
    };
  };

  config = {
    vivarium.interfaces.vec1 = lib.mkIf (config.vivarium.lan.network != null) {
      segment = config.vivarium.lan.network;
      addresses = lib.optional (config.vivarium.lan.address != null) config.vivarium.lan.address;
    };
    vivarium.nics =
      nicsOf config.vivarium.interfaces config.vivarium.index config.vivarium.mtu
        config.vivarium.segments;
    vivarium.uplinkMac = lib.toLower "52:54:00:12:00:${hex config.vivarium.index}";

    # Under mkIf, so a guest on another backend never names the kernel:
    # naming it is what builds it, and that is half an hour the first time.
    vivarium.launch = lib.mkIf (config.vivarium.backend == "uml") {
      backend = "uml";
      config = {
        kernel = "${config.system.build.umlKernel}/linux";
        bridge = lib.getExe config.system.build.umlPasstBridge;
        inherit (tools) passt;
      };
    };

    vivarium.nixDatabase.extraRoots = map toString (
      lib.attrValues config.system.build.vivariumConfigurations
    );

    assertions = [
      {
        assertion = config.vivarium.lan.address != null -> config.vivarium.lan.network != null;
        message = "vivarium.lan.address is set but vivarium.lan.network is not, so nothing would be wired to vec1.";
      }
      {
        # vec0 joins a segment fd on QEMU and UML (#958). A container's
        # uplink is pasta's, which has no such fd to redirect.
        assertion = config.vivarium.uplink.segment != null -> config.vivarium.backend != "container";
        message = "vivarium.uplink.segment needs the QEMU or UML backend; this guest is ${config.vivarium.backend}.";
      }
      {
        assertion =
          config.vivarium.uplink.segment != null
          -> (config.vivarium.segments.${config.vivarium.uplink.segment}.mtu or null) == null;
        message = "segments.${toString config.vivarium.uplink.segment}.mtu: this guest's uplink joins the segment, and an uplink takes vivarium.mtu; leave the segment's mtu null.";
      }
      {
        assertion = config.vivarium.uplink.proxy == "gvproxy" -> config.vivarium.backend == "krun";
        message = "vivarium.uplink.proxy = \"gvproxy\" needs the krun backend; this guest is ${config.vivarium.backend}.";
      }
      {
        assertion =
          config.vivarium.uplink.proxy == "gvproxy"
          -> lib.all (rule: rule.ports != "all" && rule.protocols == [ "tcp" ]) config.vivarium.forward;
        message = "vivarium.forward under gvproxy: list TCP ports only; gvproxy cannot forward `ports = \"all\"` or UDP.";
      }
      {
        assertion = lib.all (
          name:
          lib.stringLength name <= 15
          && name != "lo"
          && (name == "vec1" || builtins.match "vec[0-9]+" name == null)
        ) (lib.attrNames config.vivarium.interfaces);
        message = "vivarium.interfaces: a name is at most 15 characters, not lo, and no vecN but vec1.";
      }
      {
        assertion =
          let
            segments = map (one: one.segment) (lib.attrValues config.vivarium.interfaces);
          in
          segments == lib.unique segments;
        message = "vivarium.interfaces: two interfaces of one guest on one segment.";
      }
      {
        # Two wide rules on one address overlap on every port, and passt
        # answers that with a warning per port before carrying on -- 36000
        # lines of console for a configuration that meant one rule.
        assertion =
          let
            wide = lib.filter (rule: rule.ports == "all") config.vivarium.forward;
            addresses = map (rule: toString rule.address) wide;
          in
          addresses == lib.unique addresses;
        message = ''
          vivarium.forward has more than one `ports = "all"` rule on the same
          address (rules with `address = null` all land on the same one).
          Give them different addresses, or fold them into a single rule.
        '';
      }
      {
        assertion =
          config.vivarium.nestedVirtualization
          -> lib.elem config.vivarium.backend [
            "qemu"
            "krun"
            "container"
          ];
        message = ''
          vivarium.nestedVirtualization needs `backend = "qemu"`, "krun" or
          "container": a ${config.vivarium.backend} guest has no virtual CPU
          to expose vmx or svm on, and no host /dev/kvm to share.
        '';
      }
      {
        assertion = config.vivarium.efi -> config.vivarium.backend == "qemu";
        message = ''
          vivarium.efi needs `backend = "qemu"`: OVMF is QEMU's firmware, and a
          ${config.vivarium.backend} guest boots without firmware.
        '';
      }
      {
        assertion = config.vivarium.display.enable -> config.vivarium.backend == "qemu";
        message = ''
          vivarium.display needs `backend = "qemu"`: the screen and the input
          devices are QEMU's, and a ${config.vivarium.backend} guest has neither.
        '';
      }
    ];

    # The UML kernel is built from the same source as the guest's own
    # kernel package, so the two always agree on module versions.
    system.build = {
      vivariumRunnerPackage = pkgs.callPackage ../pkgs/vivarium-runner { };

      vivariumConfigurations = lib.mapAttrs (
        _: module: (extendModules { modules = [ module ]; }).config.system.build.toplevel
      ) config.vivarium.configurations;

      /*
        What the guest tells Nix about the store it can see -- see
        `vivarium.nixDatabase`.

        Here rather than in guest.nix on purpose. A `closureInfo` over
        `toplevel` cannot be named by anything inside `toplevel`, and a
        systemd unit is inside it; `system.build` is downstream of the
        system and nothing is built from it, so this is where the cycle
        breaks.

        Naming the roots is also what puts them in the build sandbox. A
        path Nix has been told about and cannot open is worse than one it
        does not know.

        Nothing reads this at run time; `vivariumNixDatabase` below turns it
        into the database the guest boots with.
      */
      vivariumNixRegistration = pkgs.closureInfo {
        rootPaths = [ config.system.build.toplevel ] ++ config.vivarium.nixDatabase.extraRoots;
      };

      /*
        The guest's Nix database, built here rather than loaded at boot.

        It goes on the root image under `/nix-state`, which guest.nix
        binds onto `/nix/var`, so the database is in place before anything
        runs and a guest cannot come up with an empty one.

        `config.nix.package` and not `pkgs.nix`: the file carries a schema
        version, and the Nix that reads it is the guest's.

        Three steps here are not tidiness. Each one is a bug that nixpkgs'
        `dockerTools.mkDbExtraCommand` or nix2container's
        `makeNixDatabase` hit first, and they are worth keeping in this
        order:

          * `USER` must be set, because Nix asks the environment who it is
            and fails unhelpfully when nothing answers;
          * the dump does not carry `db/schema`, and without that file Nix
            takes the store for a new one and concurrent processes race to
            initialise it;
          * `sqlite3` leaves the file in `delete` journal mode while Nix
            switches to WAL on open -- and that switch takes an exclusive
            lock, so two openers at once get `database is busy`.

        Dumping and re-importing rather than shipping the file `--load-db`
        wrote is what makes the output the same for the same closure,
        whatever order the inserts happened in.
      */
      vivariumNixDatabase =
        pkgs.runCommand "vivarium-nix-database"
          {
            nativeBuildInputs = [
              config.nix.package
              pkgs.sqlite
            ];
          }
          ''
            export USER=nobody
            export NIX_REMOTE="local?root=$PWD"
            nix-store --load-db < ${config.system.build.vivariumNixRegistration}/registration

            # A build gets no clock, so give every path the same time rather
            # than whatever this one happened to run at.
            sqlite3 nix/var/nix/db/db.sqlite 'UPDATE ValidPaths SET registrationTime = 0'
            sqlite3 nix/var/nix/db/db.sqlite '.dump' > db.dump

            mkdir -p $out
            sqlite3 $out/db.sqlite '.read db.dump'
            cp nix/var/nix/db/schema $out/schema
            sqlite3 $out/db.sqlite 'pragma journal_mode = wal' > /dev/null
          '';
    }
    # Only under the UML backend, because the kernel is half an hour the
    # first time and a QEMU guest has no use for it. `system.build` is an
    # attribute set, not options, so this is optionalAttrs rather than
    # mkIf -- an attribute that is not defined cannot be evaluated by
    # accident, which is the point.
    // lib.optionalAttrs (config.vivarium.backend == "uml") {
      umlKernel = pkgs.callPackage ../pkgs/uml-kernel {
        inherit (config.boot.kernelPackages.kernel) src version modDirVersion;
      };
      umlPasstBridge = pkgs.callPackage ../pkgs/uml-passt-bridge { };
    };
  };
}

/**
  `unix://?local-fs=false` against a daemon that serves a chroot store,
  inside the build sandbox (./patches/nix-uds-local-fs.patch).

  The chroot store holds a path the sandbox's own /nix/store does not. So
  a read through the default `unix://` fails, and the same read with
  `local-fs=false` succeeds only if it came over the socket.
*/
{
  runCommand,
  nix,
  socat,
}:
runCommand "nix-uds-local-fs-check-${nix.version}"
  {
    nativeBuildInputs = [
      nix
      socat
    ];
  }
  ''
    export HOME=$TMPDIR NIX_STATE_DIR=$TMPDIR/state NIX_CONF_DIR=$TMPDIR/conf NIX_LOG_DIR=$TMPDIR/log
    export NIX_CONFIG="extra-experimental-features = nix-command"
    remote="local?root=$TMPDIR/remote"

    echo over-the-socket > f.txt
    path=$(nix store add-file --store "$remote" ./f.txt)
    if [ -e "$path" ]; then echo "precondition: $path must not be in the sandbox store"; exit 1; fi

    socat UNIX-LISTEN:$TMPDIR/d.sock,fork EXEC:"nix daemon --stdio --store $remote" &
    # Measured on darwin: a failed run hung while socat lived on.
    trap 'kill $!' EXIT
    for _ in $(seq 100); do
      [ -S $TMPDIR/d.sock ] && break
      sleep 0.1
    done

    echo "--- unix:// reads the local store, and fails"
    if nix store cat --store "unix://$TMPDIR/d.sock" "$path" 2>err.txt; then exit 1; fi
    grep -F "$path" err.txt

    echo "--- unix://?local-fs=false: store cat, ls and copy --from"
    store="unix://$TMPDIR/d.sock?local-fs=false"
    [ "$(nix store cat --store "$store" "$path")" = over-the-socket ]
    nix store ls --store "$store" "$path"
    nix copy --no-check-sigs --from "$store" --to "local?root=$TMPDIR/pulled" "$path"
    [ "$(nix store cat --store "local?root=$TMPDIR/pulled" "$path")" = over-the-socket ]

    echo "--- a derivation reads over the socket too"
    drv=$(nix eval --store "$store" --raw --expr \
      '(derivation { name = "d"; system = "x"; builder = "/b"; }).drvPath')
    if [ -e "$drv" ]; then echo "precondition: $drv must not be in the sandbox store"; exit 1; fi
    nix derivation show --store "$store" "$drv" | grep -F '"/b"'

    touch $out
  ''

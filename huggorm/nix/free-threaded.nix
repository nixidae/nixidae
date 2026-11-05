/**
  The free-threaded interpreters of huggorm's `-ft` lanes.

  `python`
  : `python3` with the GIL disabled. Its packages skip their own tests:
    boost.python, the tree-sitter grammars, asgiref and defusedxml fail
    theirs on 3.14t, and huggorm's suite is the test that counts here.
    nanobind's CMake asks FindPython for a GIL interpreter unless told
    otherwise, and refuses 3.14t with "Wrong ABI". The fourth field of
    `Python_FIND_ABI` is the free-threaded one.

  `tsanPython`
  : The same interpreter built `--with-thread-sanitizer`. Without the GIL,
    Python code runs on two threads at once, and TSAN sees none of
    CPython's own locks unless CPython is instrumented: the plain 3.14t
    reported races in its own biased reference counting and in
    `dictresize`. Only the interpreter, with no package set: a lane builds
    on `python` and runs its suite on this one (nixvenv's `interpreter`).
    A package set over it built about 300 packages under TSAN, and an
    extension built against it takes `-fsanitize=thread` from its
    sysconfig, which the plain 3.14t cannot load ("cannot allocate memory
    in static TLS block"). No LTO: CPython's TSAN builds do not use it.

  `tsanSuppressions`
  : CPython's TSAN suppressions for a free-threaded build, from its source:
    the ones CPython's own CI uses.

  # Inputs

  `python3`
  : The GIL interpreter both derive from.
*/
{
  lib,
  runCommand,
  python3,
}:
let
  python = python3.override {
    self = python;
    pythonAttr = "${python3.pythonAttr}FreeThreading";
    enableGIL = false;
    packageOverrides =
      _: super:
      let
        untested = lib.mapAttrs (
          _: p:
          if lib.isDerivation p && p ? overridePythonAttrs then
            p.overridePythonAttrs { doCheck = false; }
          else
            p
        ) super;
      in
      untested
      // {
        nanobind = untested.nanobind.overridePythonAttrs (old: {
          cmakeFlags = old.cmakeFlags or [ ] ++ [ "-DPython_FIND_ABI=ANY;ANY;ANY;ON" ];
        });
      };
  };

  # `overrideAttrs`: the package has no argument for a configure flag.
  tsanPython =
    (python3.override {
      self = tsanPython;
      enableGIL = false;
      enableLTO = false;
    }).overrideAttrs
      (old: {
        configureFlags = old.configureFlags ++ [ "--with-thread-sanitizer" ];
        # Its own build runs it.
        env = old.env or { } // {
          TSAN_OPTIONS = "report_thread_leaks=0 suppressions=${tsanSuppressions}";
        };
      });

  tsanSuppressions = runCommand "cpython-tsan-suppressions-free-threading" { } ''
    tar --extract --to-stdout --wildcards --file ${python3.src} '*/Tools/tsan/suppressions_free_threading.txt' > "$out"
  '';
in
{
  inherit python tsanPython tsanSuppressions;
}

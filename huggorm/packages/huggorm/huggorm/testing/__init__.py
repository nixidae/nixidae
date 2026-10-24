"""pytest plugins for a suite that runs Nix through huggorm.

Load them with `-p`; each imports pytest, which huggorm does not depend on.

- `huggorm.testing.environment`: hermetic stores and daemons, the
  `--nix-test-backends` option, and the `nix_platform` marker.
- `huggorm.testing.settings`: the base settings of the pytest process, and a
  guard that fails a test which leaves a setting changed.
"""

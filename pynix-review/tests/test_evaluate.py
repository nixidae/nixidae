from __future__ import annotations

from pynix_review.evaluate import error_line

# The shape of the adscan failure on NixOS/nixpkgs#584677, trace shortened.
TRACED = """error:
       … while calling the 'derivationStrict' builtin
         at <nix/derivation-internal.nix>:37:12:

       … while evaluating derivation 'python3.14-adscan-1.0'

       error: Refusing to evaluate package 'python3.14-posthog-7.71.0' in
       /nix/store/x-source/pkgs/development/python-modules/posthog/default.nix:80 because it has problems:
       - broken: This package is broken.

       See also https://nixos.org/manual/nixpkgs/unstable#sec-problems
"""


def test_a_traced_error_gives_its_last_error_paragraph() -> None:
    assert error_line(TRACED) == (
        "error: Refusing to evaluate package 'python3.14-posthog-7.71.0' in"
        " /nix/store/x-source/pkgs/development/python-modules/posthog/default.nix:80 because it has problems:"
        " - broken: This package is broken."
    )


def test_a_one_line_error_is_itself() -> None:
    assert error_line("builder for 'x.drv' failed with exit code 1") == "builder for 'x.drv' failed with exit code 1"


def test_the_line_is_capped() -> None:
    assert len(error_line("error: " + "x" * 500)) == 300

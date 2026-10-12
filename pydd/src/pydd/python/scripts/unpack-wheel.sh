#!/usr/bin/env bash
# Unpack one wheel into the site-packages layout of the environment.
#
# The bootstrap node: nothing can install the installer, so its wheel is
# unzipped onto a path.
#
# The environment gives:
#   wheel  the `.whl` file
#   site   the site-packages directory, relative to the output
#   out    the output path
# SC2154: the node passes each variable as an attribute of the derivation.
# shellcheck disable=SC2154
set -eu

mkdir -p "$out/$site"
unzip -q -o "$wheel" -d "$out/$site"

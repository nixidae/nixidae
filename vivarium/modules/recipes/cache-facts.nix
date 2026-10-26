# Where `vivarium.recipes.cache` serves, and the key it signs with.
#
# One file, because a test also renders things outside its own evaluation
# that must name the cache: a Kubernetes manifest's nix.conf, for one. lib.nix
# exports it as `cacheFacts`.
#
# **The key is a test key, and its secret is in this repository on
# purpose.** Only guests of a run trust it, through `trustedPublicKey`; no
# real machine does. A key generated per build would need its public half at
# evaluation time, which only import-from-derivation gives.
rec {
  address = "10.254.0.1";
  port = 5000;
  url = "http://${address}:${toString port}";
  secretKeyFile = ./cache-key/secret;
  trustedPublicKey = builtins.readFile ./cache-key/public;
}

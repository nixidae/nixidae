/*
  The same questions under Kata Containers, a QEMU VM per pod, on
  either CRI. `kata` is CRI-O, as OpenShift runs it.

  By hand, not in CI, like `nested`: a GitHub runner's KVM does not
  nest again.
*/
{ kataTest, ... }:
kataTest "crio"

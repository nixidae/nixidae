{
  mkShell,
  # pkgs.jj is a JSON stream editor. jujutsu is the version control system.
  jujutsu,
  git,
}:
mkShell {
  # What it takes to work in this repository, and nothing else. Each source
  # has its own shell.nix and its own .envrc, so entering a source's directory
  # gets that source's environment.
  packages = [
    jujutsu
    git
  ];
}

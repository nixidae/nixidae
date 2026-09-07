{
  mkShell,
  umbrella,
  # pkgs.jj is a JSON stream editor. jujutsu is the version control system.
  jujutsu,
  git,
}:
mkShell {
  # What it takes to drive the umbrella, and nothing else. Each project has
  # its own shell.nix and its own .envrc, so entering a project directory
  # gets that project's environment.
  packages = [
    umbrella
    jujutsu
    git
  ];
}

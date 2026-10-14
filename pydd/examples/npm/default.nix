# An npm project: TypeScript that esbuild compiles in `npm run build`, over
# chalk 4 and ansi-styles 6. The lock nests chalk's own ansi-styles 4 under
# it. The planner reads the lock, and writes a fetch and a node for each
# package.
{
  pydd,
  runCommand,
}:
let
  demo = pydd.node.npm { project = ./project; };
in
demo
// {
  check = runCommand "npm-demo-check" { } ''
    ${demo}/bin/npm-demo > $out
    ls ${demo}/lib/node_modules/npm-demo/node_modules >> $out
  '';
}

{
  imported = import ./sub/x.nix;
  joined = builtins.readFile (./. + "/hello.txt");
  listed = builtins.readDir ./sub;
  live = builtins.readFile ./live.txt;
  copied = "${./src}";
}

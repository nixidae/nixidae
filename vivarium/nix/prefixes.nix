/*
  Address prefixes as lists of words, to find two that overlap. nixpkgs'
  `lib.network` parses IPv6 only.
*/
lib:
let
  pow2 = n: lib.foldl (x: _: x * 2) 1 (lib.range 1 n);
in
rec {
  /**
    `"a.b.c.d/n"` as `{ words; wordBits = 8; length = n; }`, or null if
    the string is no IPv4 address with a prefix length.
  */
  fromIPv4 =
    text:
    let
      parts = builtins.match "([0-9]+)\\.([0-9]+)\\.([0-9]+)\\.([0-9]+)/([0-9]+)" text;
      words = map lib.toInt (lib.take 4 parts);
      length = lib.toInt (lib.last parts);
    in
    if parts != null && lib.all (word: word < 256) words && length <= 32 then
      {
        inherit words length;
        wordBits = 8;
      }
    else
      null;

  /**
    An IPv6 address with its prefix length as `{ words; wordBits = 16; length; }`.
  */
  fromIPv6 =
    text:
    let
      parsed = lib.network.ipv6.fromString text;
    in
    {
      words = map lib.fromHexString (lib.splitString ":" parsed.address);
      wordBits = 16;
      length = parsed.prefixLength;
    };

  /**
    Whether two prefixes of one family share an address: they agree on
    every bit of the shorter one.
  */
  overlap =
    a: b:
    let
      shorter = lib.min a.length b.length;
      same =
        i:
        let
          kept = lib.max 0 (lib.min a.wordBits (shorter - i * a.wordBits));
          drop = pow2 (a.wordBits - kept);
        in
        builtins.elemAt a.words i / drop == builtins.elemAt b.words i / drop;
    in
    lib.all same (lib.range 0 (builtins.length a.words - 1));

  /**
    The pairs of `{ name; prefix; }` entries whose prefixes overlap, each
    pair once.
  */
  overlaps =
    entries:
    let
      indexed = lib.imap0 (i: entry: entry // { inherit i; }) entries;
    in
    lib.concatMap (
      a:
      map (b: [
        a
        b
      ]) (lib.filter (b: b.i > a.i && overlap a.prefix b.prefix) indexed)
    ) indexed;
}

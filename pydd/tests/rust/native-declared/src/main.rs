fn main() {
    let packed = zstd::encode_all(&b"hello from native-declared"[..], 3).unwrap();
    let text = String::from_utf8(zstd::decode_all(&packed[..]).unwrap()).unwrap();
    let digest = openssl::sha::sha256(text.as_bytes());
    println!("{} sha256={:02x}{:02x} openssl={}", text, digest[0], digest[1], openssl::version::number() >> 28);
}

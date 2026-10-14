/// The bytes, through zstd and back.
///
/// ```
/// assert_eq!(cargo_demo::roundtrip(b"abc"), b"abc");
/// ```
pub fn roundtrip(bytes: &[u8]) -> Vec<u8> {
    zstd::decode_all(&zstd::encode_all(bytes, 3).unwrap()[..]).unwrap()
}

/// The first two bytes of the SHA-256 of `bytes`, from OpenSSL, in hex.
pub fn digest(bytes: &[u8]) -> String {
    let digest = openssl::sha::sha256(bytes);
    format!("{:02x}{:02x}", digest[0], digest[1])
}

#[cfg(test)]
mod tests {
    #[test]
    fn digest_of_nothing() {
        assert_eq!(super::digest(b""), "e3b0");
    }
}

fn main() {
    let text = String::from_utf8(cargo_demo::roundtrip(b"hello from cargo-demo")).unwrap();
    println!("{} sha256={}", text, cargo_demo::digest(text.as_bytes()));
}

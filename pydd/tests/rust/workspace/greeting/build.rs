use std::{env, fs, path::Path};

fn main() {
    let out = env::var("OUT_DIR").unwrap();
    fs::write(Path::new(&out).join("text.rs"), "\"hello from the workspace\"").unwrap();
}

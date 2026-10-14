use std::{env, fs, path::Path};

fn main() {
    let out = env::var("OUT_DIR").unwrap();
    fs::write(Path::new(&out).join("text.rs"), "\"hello from the workspace\"").unwrap();
    // A package with `links` gives metadata to the build scripts of the
    // packages that depend on it, as DEP_GREETING_<KEY>.
    println!("cargo::metadata=source=greeting/build.rs");
}

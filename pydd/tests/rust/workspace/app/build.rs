use std::{env, process::Command};

fn main() {
    let source = env::var("DEP_GREETING_SOURCE").expect("greeting's metadata");
    println!("cargo::rustc-env=GREETING_SOURCE={source}");
    let cargo = env::var("CARGO").expect("CARGO");
    assert!(Command::new(cargo).arg("--version").status().unwrap().success());
    // The binaries of this package get it; its tests do not.
    println!("cargo::rustc-link-arg-bins=-Wl,-rpath,/pydd-link-arg-bins");
}

#[test]
fn prints_the_greeting() {
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_ws-app")).output().unwrap();
    assert_eq!(String::from_utf8(output.stdout).unwrap(), "HELLO FROM THE WORKSPACE 42 (greeting/build.rs)\n");
}

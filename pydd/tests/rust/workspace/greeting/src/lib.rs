const TEXT: &str = include!(concat!(env!("OUT_DIR"), "/text.rs"));

pub fn greet() -> String {
    if cfg!(feature = "shout") {
        TEXT.to_uppercase()
    } else {
        TEXT.to_string()
    }
}

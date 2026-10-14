const TEXT: &str = include!(concat!(env!("OUT_DIR"), "/text.rs"));

/// The greeting, in capitals with the `shout` feature.
///
/// ```
/// assert!(greeting::greet().to_lowercase().contains("workspace"));
/// ```
pub fn greet() -> String {
    if cfg!(feature = "shout") {
        TEXT.to_uppercase()
    } else {
        TEXT.to_string()
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn reads_the_build_script() {
        assert_eq!(super::TEXT, "hello from the workspace");
    }
}

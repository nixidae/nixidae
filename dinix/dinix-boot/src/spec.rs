use std::fmt;
use std::path::PathBuf;

/// One step before dinit starts.
#[derive(Debug, PartialEq, Eq)]
pub enum Step {
    Dir {
        path: PathBuf,
        mode: u32,
    },
    Mount {
        source: String,
        target: PathBuf,
        fstype: String,
        /// Flag words such as `ro`, as mount(2) flags.
        flags: u64,
        /// The rest of the options, as the filesystem's own data.
        data: String,
        /// `mount?`: a mount that may fail, such as a share a host did not
        /// give. Its failure is a line on the console, not the end of boot.
        optional: bool,
    },
    /// Replace this process. Always the last step.
    Exec { program: PathBuf, args: Vec<String> },
}

#[derive(Debug)]
pub struct ParseError {
    pub line_number: usize,
    pub message: String,
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "line {}: {}", self.line_number, self.message)
    }
}

// From include/uapi/linux/mount.h; the numbers are ABI.
const MS_RDONLY: u64 = 1;
const MS_NOSUID: u64 = 2;
const MS_NODEV: u64 = 4;
const MS_NOEXEC: u64 = 8;
const MS_NOATIME: u64 = 1024;
const MS_RELATIME: u64 = 1 << 21;

/// Split mount options into mount(2) flags and the filesystem's data, as
/// mount(8) does for the flag words it knows. `-` is no options.
fn options(field: &str) -> (u64, String) {
    let mut flags = 0;
    let mut data = Vec::new();
    for option in field.split(',').filter(|o| !o.is_empty() && *o != "-") {
        match option {
            "ro" => flags |= MS_RDONLY,
            "rw" => {}
            "nosuid" => flags |= MS_NOSUID,
            "nodev" => flags |= MS_NODEV,
            "noexec" => flags |= MS_NOEXEC,
            "noatime" => flags |= MS_NOATIME,
            "relatime" => flags |= MS_RELATIME,
            other => data.push(other),
        }
    }
    (flags, data.join(","))
}

/// Parse a spec: one step per line, tab separated, as dinix-init's, because
/// Nix writes it. `#` starts a comment; blank lines are skipped. Every field
/// takes dinit's `${NAME}` substitution, so a value the kernel command line
/// carries (an unknown `name=value` reaches PID 1 as a variable) reaches a
/// step. Shown with the tabs as `->`:
///
/// ```text
/// dir    -> /host/nix -> 0755
/// mount  -> nix       -> /host/nix -> virtiofs -> ro,dax=always
/// mount  -> overlay   -> /nix      -> overlay  -> lowerdir=/host/nix,upperdir=/.nix-upper,workdir=/.nix-work
/// mount? -> artifacts -> /artifacts -> virtiofs -> -
/// exec   -> /nix/store/...-dinit/bin/dinit -> --system
/// ```
///
/// Exactly one `exec`, and it is last: the binary hands PID 1 to it.
pub fn parse(input: &str) -> Result<Vec<Step>, ParseError> {
    parse_with(input, &|name| std::env::var(name).ok())
}

/// `parse`, reading variables through `lookup`, so a test does not depend on
/// its environment.
pub fn parse_with(input: &str, lookup: &dyn Fn(&str) -> Option<String>) -> Result<Vec<Step>, ParseError> {
    let mut steps = Vec::new();
    let mut last = 0;

    for (index, raw) in input.lines().enumerate() {
        let line_number = index + 1;
        let line = raw.trim_end_matches(['\r']);
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        last = line_number;
        let fail = |message: String| ParseError { line_number, message };
        if matches!(steps.last(), Some(Step::Exec { .. })) {
            return Err(fail("a step after exec, which never returns".to_string()));
        }
        let fields = line
            .split('\t')
            .map(|field| crate::expand::expand(field, lookup).map_err(|error| fail(error.message)))
            .collect::<Result<Vec<String>, ParseError>>()?;
        let want = |count: usize, names: &str| {
            if fields.len() - 1 == count {
                Ok(())
            } else {
                Err(fail(format!("{} takes {count} fields ({names}), got {}", fields[0], fields.len() - 1)))
            }
        };

        match fields[0].as_str() {
            "dir" => {
                want(2, "path, mode")?;
                steps.push(Step::Dir {
                    path: PathBuf::from(&fields[1]),
                    mode: u32::from_str_radix(&fields[2], 8)
                        .map_err(|_| fail(format!("mode {:?} is not octal", fields[2])))?,
                });
            }
            "mount" | "mount?" => {
                want(4, "source, target, fstype, options")?;
                let (flags, data) = options(&fields[4]);
                steps.push(Step::Mount {
                    source: fields[1].clone(),
                    target: PathBuf::from(&fields[2]),
                    fstype: fields[3].clone(),
                    flags,
                    data,
                    optional: fields[0] == "mount?",
                });
            }
            "exec" => {
                if fields.len() < 2 || fields[1].is_empty() {
                    return Err(fail("exec takes a program".to_string()));
                }
                steps.push(Step::Exec {
                    program: PathBuf::from(&fields[1]),
                    args: fields[2..].to_vec(),
                });
            }
            other => return Err(fail(format!("unknown step {other:?}"))),
        }
    }

    if !matches!(steps.last(), Some(Step::Exec { .. })) {
        return Err(ParseError {
            line_number: last,
            message: "the spec ends without exec: PID 1 would have nothing to become".to_string(),
        });
    }
    Ok(steps)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn none(_: &str) -> Option<String> {
        None
    }

    #[test]
    fn parses_a_boot() {
        let spec = "# the store\n\
            dir\t/host/nix\t0755\n\
            mount\tnix\t/host/nix\tvirtiofs\tro,dax=always\n\
            mount?\tartifacts\t/artifacts\tvirtiofs\t-\n\
            exec\t/bin/dinit\t--system\n";
        assert_eq!(
            parse_with(spec, &none).unwrap(),
            vec![
                Step::Dir {
                    path: PathBuf::from("/host/nix"),
                    mode: 0o755,
                },
                Step::Mount {
                    source: "nix".to_string(),
                    target: PathBuf::from("/host/nix"),
                    fstype: "virtiofs".to_string(),
                    flags: MS_RDONLY,
                    data: "dax=always".to_string(),
                    optional: false,
                },
                Step::Mount {
                    source: "artifacts".to_string(),
                    target: PathBuf::from("/artifacts"),
                    fstype: "virtiofs".to_string(),
                    flags: 0,
                    data: String::new(),
                    optional: true,
                },
                Step::Exec {
                    program: PathBuf::from("/bin/dinit"),
                    args: vec!["--system".to_string()],
                },
            ]
        );
    }

    #[test]
    fn takes_a_value_from_the_kernel_command_line() {
        let lookup = |name: &str| (name == "VIVARIUM_STORE").then(|| "/run/view".to_string());
        let steps = parse_with("mount\tnone\t/host/nix\thostfs\t${VIVARIUM_STORE}\nexec\t/init", &lookup).unwrap();
        assert!(matches!(&steps[0], Step::Mount { data, .. } if data == "/run/view"));
    }

    #[test]
    fn refuses_a_spec_with_no_exec() {
        let error = parse_with("dir\t/run\t0755\n", &none).unwrap_err();
        assert!(error.message.contains("without exec"), "{error}");
    }

    #[test]
    fn refuses_a_step_after_exec() {
        let error = parse_with("exec\t/init\ndir\t/run\t0755\n", &none).unwrap_err();
        assert_eq!(error.line_number, 2);
    }

    #[test]
    fn refuses_a_short_mount() {
        assert!(parse_with("mount\tnix\t/nix\tvirtiofs\nexec\t/init", &none).is_err());
    }
}

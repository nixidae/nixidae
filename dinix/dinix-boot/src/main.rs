//! A VM's first process: mounts what dinit needs, then becomes dinit.
//!
//! The kernel runs `init=` before the Nix store is mounted, so this binary is
//! static and lives on the root disk, and dinit, which is in the store, cannot
//! be the kernel's init itself. Everything it does comes from a spec Nix
//! writes (src/spec.rs): directories, mounts in order, then one `exec`. It
//! knows nothing of any VM host; the spec says what to mount.
//!
//! dinix-init stays the binary that can exec nothing. This one exists to exec,
//! once, as its last step, and so it is a crate of its own.
//!
//!     init=/dinix-boot DINIX_SPEC=/dinix-boot.spec
//!
//! The spec's path is the first argument, else `DINIX_SPEC` from the kernel
//! command line, which the kernel hands PID 1 as a variable (not with a dot in
//! its name: the kernel takes that for a module's parameter and keeps it),
//! else `/dinix-boot.spec`.

use std::ffi::{CString, OsStr};
use std::fs;
use std::os::raw::{c_char, c_int, c_ulong, c_void};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Command, ExitCode};

#[path = "../../dinix-init/src/expand.rs"]
mod expand;
mod spec;

use spec::Step;

extern "C" {
    fn mount(
        source: *const c_char,
        target: *const c_char,
        fstype: *const c_char,
        flags: c_ulong,
        data: *const c_void,
    ) -> c_int;
}

const SPEC_VARIABLE: &str = "DINIX_SPEC";
const DEFAULT_SPEC: &str = "/dinix-boot.spec";

fn c_string(value: &[u8]) -> Result<CString, String> {
    CString::new(value).map_err(|_| format!("{:?} holds a NUL byte", String::from_utf8_lossy(value)))
}

fn mount_one(source: &str, target: &Path, fstype: &str, flags: u64, data: &str) -> Result<(), String> {
    let source = c_string(source.as_bytes())?;
    let target_c = c_string(target.as_os_str().as_bytes())?;
    let fstype = c_string(fstype.as_bytes())?;
    let data = c_string(data.as_bytes())?;
    // SAFETY: every pointer is a NUL-terminated string that outlives the call.
    let result = unsafe {
        mount(
            source.as_ptr(),
            target_c.as_ptr(),
            fstype.as_ptr(),
            flags as c_ulong,
            data.as_ptr().cast(),
        )
    };
    if result == 0 {
        Ok(())
    } else {
        Err(format!("mounting on {}: {}", target.display(), std::io::Error::last_os_error()))
    }
}

/// Run every step but the last, and return the last: the program to become.
fn run(steps: Vec<Step>) -> Result<Command, String> {
    for step in steps {
        match step {
            Step::Dir { path, mode } => {
                fs::create_dir_all(&path).map_err(|error| format!("making {}: {error}", path.display()))?;
                fs::set_permissions(&path, fs::Permissions::from_mode(mode))
                    .map_err(|error| format!("setting the mode of {}: {error}", path.display()))?;
            }
            Step::Mount {
                source,
                target,
                fstype,
                flags,
                data,
                optional,
            } => {
                fs::create_dir_all(&target).map_err(|error| format!("making {}: {error}", target.display()))?;
                match mount_one(&source, &target, &fstype, flags, &data) {
                    Ok(()) => {}
                    Err(error) if optional => eprintln!("dinix-boot: {error}; it is optional, so going on"),
                    Err(error) => return Err(error),
                }
            }
            Step::Exec { program, args } => {
                let mut command = Command::new(program);
                command.args(args);
                return Ok(command);
            }
        }
    }
    Err("the spec has no exec".to_string())
}

fn main() -> ExitCode {
    let spec_path = std::env::args_os()
        .nth(1)
        .or_else(|| std::env::var_os(SPEC_VARIABLE))
        .unwrap_or_else(|| OsStr::new(DEFAULT_SPEC).to_os_string());
    let spec_path = Path::new(&spec_path);

    let steps = match fs::read_to_string(spec_path)
        .map_err(|error| format!("reading {}: {error}", spec_path.display()))
        .and_then(|source| spec::parse(&source).map_err(|error| format!("{}: {error}", spec_path.display())))
    {
        Ok(steps) => steps,
        Err(error) => {
            eprintln!("dinix-boot: {error}");
            return ExitCode::FAILURE;
        }
    };

    match run(steps) {
        // exec returns only on failure.
        Ok(mut command) => {
            let error = command.exec();
            eprintln!("dinix-boot: exec {:?}: {error}", command.get_program());
            ExitCode::FAILURE
        }
        Err(error) => {
            eprintln!("dinix-boot: {error}");
            ExitCode::FAILURE
        }
    }
}

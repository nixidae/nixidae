//! darwin-virtiofsd: a vhost-user virtio-fs server for macOS hosts.
//!
//!   darwin-virtiofsd --socket-path PATH --shared-dir DIR
//!                    [--allowlist FILE] [--negative-timeout SECONDS] [--writable]
//!
//! A share is read-only unless `--writable`. `--allowlist` shows the guest only the
//! paths FILE names, one a line, and reads FILE again when a lookup misses and FILE
//! changed. On a writable share the directories above an allowed path are fixed
//! (`fs/filtered.rs`).
//!
//! The FUSE side is libkrun's (`src/fs`, `src/virtio`, from containers/libkrun
//! a0375321bb1e; the allowlist from github.com/nixidae/libkrun 28fc6bc8). It speaks
//! Linux's FUSE ABI, which is also what macOS guests' AppleVirtIOFS expects:
//! Virtualization.framework serves both from one server.

#[macro_use]
extern crate log;

mod fs;
mod utils;
mod vhost_user;
mod virtio;

use std::os::unix::net::UnixListener;
use std::path::Path;
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

use fs::allowlist::Allowlist;
use fs::passthrough::Config;
use fs::{CachePolicy, FileSystem, PassthroughFsFiltered, PassthroughFsRo, PermissionSemantics};

// The copied modules reach these by libkrun's relative paths.
use virtio::linux_errno;

/// A Nix store path never changes, so the guest may keep what it learned.
const STORE_TIMEOUT: Duration = Duration::from_secs(24 * 60 * 60);

const USAGE: &str = "usage: darwin-virtiofsd --socket-path PATH --shared-dir DIR \
                     [--allowlist FILE] [--negative-timeout SECONDS] [--writable]";

struct Args {
    socket: String,
    shared: String,
    allowlist: Option<String>,
    negative_timeout: Duration,
    writable: bool,
}

fn parse() -> Option<Args> {
    let (mut socket, mut shared, mut allowlist) = (None, None, None);
    let mut negative_timeout = Duration::ZERO;
    let mut writable = false;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--socket-path" => socket = args.next(),
            "--shared-dir" => shared = args.next(),
            "--allowlist" => allowlist = args.next(),
            "--negative-timeout" => {
                negative_timeout = Duration::from_secs(args.next()?.parse().ok()?);
            }
            "--writable" => writable = true,
            _ => return None,
        }
    }
    Some(Args {
        socket: socket?,
        shared: shared?,
        allowlist,
        negative_timeout,
        writable,
    })
}

fn main() -> ExitCode {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("warn")).init();
    let Some(args) = parse() else {
        eprintln!("{USAGE}");
        return ExitCode::from(2);
    };
    let allowlist = match &args.allowlist {
        None => None,
        Some(file) => match Allowlist::from_file(Path::new(&args.shared), Path::new(file)) {
            Ok(allowlist) => Some(Arc::new(allowlist)),
            Err(e) => {
                eprintln!("darwin-virtiofsd: {file}: {e}");
                return ExitCode::FAILURE;
            }
        },
    };
    let inodes = Arc::new(fs::InodeAllocator::new());
    let served = if args.writable {
        // LinuxComplete keeps the guest's owner and mode in an xattr and gives the host
        // file 0600 or 0700 (measured); the host reads a build's files as they are.
        let cfg = Config {
            root_dir: args.shared.clone(),
            semantics: PermissionSemantics::LinuxSimplified,
            ..Default::default()
        };
        PassthroughFsFiltered::new(cfg, inodes, allowlist, args.negative_timeout)
            .map(|fs| serve(&args.socket, fs))
    } else {
        let cfg = Config {
            root_dir: args.shared.clone(),
            cache_policy: CachePolicy::Always,
            entry_timeout: STORE_TIMEOUT,
            attr_timeout: STORE_TIMEOUT,
            ..Default::default()
        };
        PassthroughFsRo::new(cfg, inodes, allowlist, args.negative_timeout)
            .map(|fs| serve(&args.socket, fs))
    };
    match served {
        Ok(code) => code,
        Err(e) => {
            eprintln!("darwin-virtiofsd: {e}");
            ExitCode::FAILURE
        }
    }
}

fn serve<F: FileSystem + Sync + Send + 'static>(socket: &str, fs: F) -> ExitCode {
    let mut backend = vhost_user::Backend::new(fs::server::Server::new(fs));
    let _ = std::fs::remove_file(socket);
    let listener = match UnixListener::bind(socket) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("darwin-virtiofsd: {socket}: {e}");
            return ExitCode::FAILURE;
        }
    };
    let stream = match listener.accept() {
        Ok((stream, _)) => stream,
        Err(e) => {
            eprintln!("darwin-virtiofsd: accept: {e}");
            return ExitCode::FAILURE;
        }
    };
    match backend.serve(stream) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("darwin-virtiofsd: {e}");
            ExitCode::FAILURE
        }
    }
}

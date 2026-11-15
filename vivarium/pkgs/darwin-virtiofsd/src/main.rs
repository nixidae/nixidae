//! darwin-virtiofsd: a read-only vhost-user virtio-fs server for macOS hosts.
//!
//!   darwin-virtiofsd --socket-path PATH --shared-dir DIR
//!
//! The FUSE side is libkrun's (`src/fs`, `src/virtio`, from containers/libkrun
//! a0375321bb1e). It speaks Linux's FUSE ABI, which is also what macOS guests'
//! AppleVirtIOFS expects: Virtualization.framework serves both from one server.

#[macro_use]
extern crate log;

mod fs;
mod utils;
mod vhost_user;
mod virtio;

use std::os::unix::net::UnixListener;
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

use fs::passthrough::Config;
use fs::{CachePolicy, PassthroughFsRo};

// The copied modules reach these by libkrun's relative paths.
use virtio::linux_errno;

/// A Nix store path never changes, so the guest may keep what it learned.
const STORE_TIMEOUT: Duration = Duration::from_secs(24 * 60 * 60);

fn main() -> ExitCode {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("warn")).init();
    let mut socket = None;
    let mut shared = None;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--socket-path" => socket = args.next(),
            "--shared-dir" => shared = args.next(),
            _ => {
                eprintln!("usage: darwin-virtiofsd --socket-path PATH --shared-dir DIR");
                return ExitCode::from(2);
            }
        }
    }
    let (Some(socket), Some(shared)) = (socket, shared) else {
        eprintln!("usage: darwin-virtiofsd --socket-path PATH --shared-dir DIR");
        return ExitCode::from(2);
    };

    let cfg = Config {
        root_dir: shared,
        cache_policy: CachePolicy::Always,
        entry_timeout: STORE_TIMEOUT,
        attr_timeout: STORE_TIMEOUT,
        ..Default::default()
    };
    let fs = match PassthroughFsRo::new(cfg, Arc::new(fs::InodeAllocator::new())) {
        Ok(fs) => fs,
        Err(e) => {
            eprintln!("darwin-virtiofsd: {e}");
            return ExitCode::FAILURE;
        }
    };
    let mut backend = vhost_user::Backend::new(fs::server::Server::new(fs));

    let _ = std::fs::remove_file(&socket);
    let listener = match UnixListener::bind(&socket) {
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

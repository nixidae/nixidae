// libkrun's devices/src/virtio/fs/mod.rs (Apache-2.0), cut to what a
// vhost-user server on a macOS host needs.
#![allow(dead_code, non_camel_case_types, unexpected_cfgs)]

#[allow(dead_code)]
mod filesystem;
pub mod fuse;
mod inode_alloc;
pub mod macos;
#[allow(dead_code)]
mod multikey;
mod read_only;
pub mod server;

pub use inode_alloc::InodeAllocator;
pub use macos::fs_utils;
pub use macos::passthrough;
pub use read_only::PassthroughFsRo;

use super::virtio::bindings;
use super::virtio::descriptor_utils;

#[repr(u32)]
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub enum CachePolicy {
    Never = 0,
    #[default]
    Auto = 1,
    Always = 2,
}

#[repr(u32)]
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub enum PermissionSemantics {
    #[default]
    LinuxComplete = 0,
    LinuxSimplified = 1,
}

use std::ffi::{FromBytesWithNulError, FromVecWithNulError};
use std::io;

use descriptor_utils::Error as DescriptorError;

#[derive(Debug)]
pub enum FsError {
    /// The host does not implement the selected permission semantics.
    UnsupportedPermissionSemantics,
    /// Failed to decode protocol messages.
    DecodeMessage(io::Error),
    /// Failed to encode protocol messages.
    EncodeMessage(io::Error),
    /// The guest failed to send a require extensions.
    MissingExtension,
    /// One or more parameters are missing.
    MissingParameter,
    /// A C string parameter is invalid.
    InvalidCString(FromBytesWithNulError),
    InvalidCString2(FromVecWithNulError),
    /// The `len` field of the header is too small.
    InvalidHeaderLength,
    /// The `size` field of the `SetxattrIn` message does not match the length
    /// of the decoded value.
    InvalidXattrSize((u32, usize)),
    QueueReader(DescriptorError),
    QueueWriter(DescriptorError),
}

type Result<T> = std::result::Result<T, FsError>;

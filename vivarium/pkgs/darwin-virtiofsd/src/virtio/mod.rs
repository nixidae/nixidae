// Modules from libkrun's devices/src/virtio (Apache-2.0, and BSD where a
// file says so). They stay close to libkrun's text, so libkrun's unused
// parts, names and `blk` feature gates stay too.
#![allow(dead_code, non_camel_case_types, unexpected_cfgs)]

pub mod bindings;
pub mod descriptor_utils;
pub mod file_traits;
pub mod linux_errno;
pub mod queue;

/// libkrun's DAX window; see `crate::utils`.
#[allow(dead_code)]
pub struct VirtioShmRegion {
    pub host_addr: u64,
    pub guest_addr: u64,
    pub size: usize,
}

//! What the copied libkrun modules name from libkrun's `utils` crate.
//!
//! libkrun maps DAX windows by sending these to its VMM. This server offers
//! no DAX window, so it never sends one: `map_sender` and `shm_region` are
//! always `None`.

pub mod worker_message {
    #[allow(dead_code)]
    #[derive(Debug)]
    pub enum WorkerMessage {
        GpuAddMapping(crossbeam_channel::Sender<bool>, u64, u64, u64),
        GpuRemoveMapping(crossbeam_channel::Sender<bool>, u64, u64),
    }
}

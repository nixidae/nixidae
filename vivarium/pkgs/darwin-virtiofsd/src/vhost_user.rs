//! The backend side of vhost-user, as QEMU's `vhost-user-fs-pci` drives it.
//!
//! Not rust-vmm's `vhost` crate: 0.18.1 needs Linux (`eventfd`, `SO_DOMAIN`).
//! QEMU on a host without eventfd hands each notifier over as a pipe: the
//! kick is a read end, the call a write end (`util/event_notifier-posix.c`).

use std::fs::File;
use std::io::{self, IoSliceMut, Read, Write};
use std::num::Wrapping;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use nix::cmsg_space;
use nix::poll::{PollFd, PollFlags, PollTimeout, poll};
use nix::sys::socket::{ControlMessageOwned, MsgFlags, recvmsg};
use vm_memory::{
    FileOffset, GuestAddress, GuestMemoryMmap, GuestMemoryRegion, GuestRegionMmap, MmapRegion,
};

use crate::fs::FileSystem;
use crate::fs::fuse::{InHeader, Opcode};
use crate::fs::server::Server;
use crate::virtio::descriptor_utils::{Reader, Writer};
use crate::virtio::queue::{DescriptorChain, Queue};

const VIRTIO_F_VERSION_1: u64 = 1 << 32;
const VIRTIO_RING_F_INDIRECT_DESC: u64 = 1 << 28;
/// Dirty logging; QEMU blocks migration without it and LOG_SHMFD.
const VHOST_F_LOG_ALL: u64 = 1 << 26;
const PROTOCOL_F_LOG_SHMFD: u64 = 1 << 1;
const LOG_PAGE: u64 = 0x1000;
const VHOST_USER_F_PROTOCOL_FEATURES: u64 = 1 << 30;
const PROTOCOL_F_MQ: u64 = 1 << 0;
const PROTOCOL_F_REPLY_ACK: u64 = 1 << 3;
/// QEMU's vhost-user-fs saves only with this (`vuf_check_migration_support`).
const PROTOCOL_F_DEVICE_STATE: u64 = 1 << 19;

/// What `SET_DEVICE_STATE_FD` streams: no FUSE session, nothing else.
const DEVICE_STATE: &[u8; 8] = b"dvfsd\0\0\x01";
const TRANSFER_SAVE: u32 = 0;

const FLAG_VERSION: u32 = 0x1;
const FLAG_REPLY: u32 = 0x4;
const FLAG_NEED_REPLY: u32 = 0x8;
const VRING_NOFD: u64 = 0x100;
const MAX_REGIONS: usize = 8;

/// One high-priority queue and one request queue.
pub const NUM_QUEUES: usize = 2;
const QUEUE_MAX_SIZE: u16 = 1024;

#[repr(u32)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Request {
    GetFeatures = 1,
    SetFeatures = 2,
    SetOwner = 3,
    ResetOwner = 4,
    SetMemTable = 5,
    SetLogBase = 6,
    SetLogFd = 7,
    SetVringNum = 8,
    SetVringAddr = 9,
    SetVringBase = 10,
    GetVringBase = 11,
    SetVringKick = 12,
    SetVringCall = 13,
    SetVringErr = 14,
    GetProtocolFeatures = 15,
    SetProtocolFeatures = 16,
    GetQueueNum = 17,
    SetVringEnable = 18,
    SetDeviceStateFd = 42,
    CheckDeviceState = 43,
}

impl Request {
    fn from_u32(value: u32) -> Option<Self> {
        use Request::*;
        Some(match value {
            1 => GetFeatures,
            2 => SetFeatures,
            3 => SetOwner,
            4 => ResetOwner,
            5 => SetMemTable,
            6 => SetLogBase,
            7 => SetLogFd,
            8 => SetVringNum,
            9 => SetVringAddr,
            10 => SetVringBase,
            11 => GetVringBase,
            12 => SetVringKick,
            13 => SetVringCall,
            14 => SetVringErr,
            15 => GetProtocolFeatures,
            16 => SetProtocolFeatures,
            17 => GetQueueNum,
            18 => SetVringEnable,
            42 => SetDeviceStateFd,
            43 => CheckDeviceState,
            _ => return None,
        })
    }
}

/// QEMU's dirty log: one bit for each 4 KiB page of guest physical memory.
struct DirtyLog {
    base: *mut u8,
    len: usize,
}

// The bitmap is shared memory that every writer updates with atomic ORs.
unsafe impl Send for DirtyLog {}
unsafe impl Sync for DirtyLog {}

impl DirtyLog {
    fn map(fd: OwnedFd, size: u64, offset: u64) -> io::Result<Self> {
        let len = usize::try_from(size).map_err(io::Error::other)?;
        // A fresh shared mapping of the descriptor QEMU sent; nothing else aliases it here.
        let base = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                len,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_SHARED,
                fd.as_raw_fd(),
                offset as libc::off_t,
            )
        };
        if base == libc::MAP_FAILED {
            return Err(io::Error::last_os_error());
        }
        Ok(DirtyLog { base: base.cast(), len })
    }

    fn mark(&self, gpa: u64, len: u64) {
        if len == 0 {
            return;
        }
        for page in gpa / LOG_PAGE..=(gpa + len - 1) / LOG_PAGE {
            let byte = (page / 8) as usize;
            if byte >= self.len {
                break;
            }
            // In bounds, and QEMU reads the bitmap with atomics too.
            let cell = unsafe { std::sync::atomic::AtomicU8::from_ptr(self.base.add(byte)) };
            cell.fetch_or(1 << (page % 8), Ordering::Relaxed);
        }
    }
}

impl Drop for DirtyLog {
    fn drop(&mut self) {
        // The mapping `map` made, unmapped once.
        unsafe { libc::munmap(self.base.cast(), self.len) };
    }
}

#[derive(Clone, Copy)]
struct MemRegion {
    guest_phys_addr: u64,
    memory_size: u64,
    userspace_addr: u64,
}

struct Vring {
    queue: Arc<Mutex<Queue>>,
    /// QEMU's virtual addresses of the descriptor table, used and available rings.
    addr: Option<(u64, u64, u64)>,
    base: u16,
    call: Arc<Mutex<Option<File>>>,
    enabled: Arc<AtomicBool>,
    worker: Option<(JoinHandle<()>, OwnedFd)>,
}

impl Vring {
    fn new() -> Self {
        Vring {
            queue: Arc::new(Mutex::new(Queue::new(QUEUE_MAX_SIZE))),
            addr: None,
            base: 0,
            call: Arc::new(Mutex::new(None)),
            enabled: Arc::new(AtomicBool::new(false)),
            worker: None,
        }
    }

    fn stop(&mut self) {
        if let Some((handle, stop)) = self.worker.take() {
            drop(stop);
            if handle.join().is_err() {
                error!("a queue worker panicked");
            }
        }
    }
}

pub struct Backend<F: FileSystem + Sync + Send + 'static> {
    server: Arc<Server<F>>,
    /// Shared with the workers: SET_FEATURES turns VHOST_F_LOG_ALL on and off.
    features: Arc<AtomicU64>,
    log: Arc<Mutex<Option<Arc<DirtyLog>>>>,
    protocol_features: u64,
    regions: Vec<MemRegion>,
    mem: Option<Arc<GuestMemoryMmap>>,
    vrings: Vec<Vring>,
    exit_code: Arc<AtomicI32>,
    /// Between FUSE_INIT and FUSE_DESTROY: the guest holds inodes and handles
    /// that only this process knows, so its state cannot move.
    session: Arc<AtomicBool>,
    transfer: Option<JoinHandle<io::Result<()>>>,
}

impl<F: FileSystem + Sync + Send + 'static> Backend<F> {
    pub fn new(server: Server<F>) -> Self {
        Backend {
            server: Arc::new(server),
            features: Arc::new(AtomicU64::new(0)),
            log: Arc::new(Mutex::new(None)),
            protocol_features: 0,
            regions: Vec::new(),
            mem: None,
            vrings: (0..NUM_QUEUES).map(|_| Vring::new()).collect(),
            exit_code: Arc::new(AtomicI32::new(0)),
            session: Arc::new(AtomicBool::new(false)),
            transfer: None,
        }
    }

    /// Saves or loads the device state through `pipe`, on a thread: QEMU
    /// writes or reads its end only after the reply.
    fn set_device_state_fd(&mut self, payload: &[u8], pipe: OwnedFd) -> io::Result<()> {
        let direction = u32_at(payload, 0)?;
        if direction == TRANSFER_SAVE && self.session.load(Ordering::Acquire) {
            return Err(io::Error::other(
                "the guest has the share mounted; unmount it before a save",
            ));
        }
        let mut pipe = File::from(pipe);
        self.transfer = Some(std::thread::spawn(move || {
            if direction == TRANSFER_SAVE {
                pipe.write_all(DEVICE_STATE)
            } else {
                let mut state = Vec::new();
                pipe.read_to_end(&mut state)?;
                if state != DEVICE_STATE {
                    return Err(io::Error::other(format!("unknown device state {state:?}")));
                }
                Ok(())
            }
        }));
        Ok(())
    }

    fn check_device_state(&mut self) -> io::Result<()> {
        let transfer = self
            .transfer
            .take()
            .ok_or_else(|| io::Error::other("no device state transfer"))?;
        transfer
            .join()
            .map_err(|_| io::Error::other("the device state transfer panicked"))?
    }

    /// Serves one frontend until it disconnects.
    pub fn serve(&mut self, mut stream: UnixStream) -> io::Result<()> {
        loop {
            let mut header = [0u8; 12];
            let mut fds: Vec<OwnedFd> = Vec::new();
            let got = {
                let mut iov = [IoSliceMut::new(&mut header)];
                let mut space = cmsg_space!([RawFd; MAX_REGIONS]);
                let msg = recvmsg::<()>(
                    stream.as_raw_fd(),
                    &mut iov,
                    Some(&mut space),
                    // macOS has no MSG_CMSG_CLOEXEC; this process execs nothing.
                    MsgFlags::empty(),
                )
                .map_err(io::Error::from)?;
                for cmsg in msg.cmsgs().map_err(io::Error::from)? {
                    if let ControlMessageOwned::ScmRights(received) = cmsg {
                        // recvmsg handed these descriptors to this process.
                        fds.extend(received.into_iter().map(|fd| unsafe { OwnedFd::from_raw_fd(fd) }));
                    }
                }
                msg.bytes
            };
            if got == 0 {
                info!("the frontend disconnected");
                return Ok(());
            }
            if got < header.len() {
                stream.read_exact(&mut header[got..])?;
            }
            let request = u32::from_le_bytes(header[0..4].try_into().unwrap());
            let flags = u32::from_le_bytes(header[4..8].try_into().unwrap());
            let size = u32::from_le_bytes(header[8..12].try_into().unwrap()) as usize;
            let mut payload = vec![0u8; size];
            stream.read_exact(&mut payload)?;

            let Some(request) = Request::from_u32(request) else {
                warn!("unhandled vhost-user request {request} ({size} bytes)");
                if flags & FLAG_NEED_REPLY != 0 {
                    reply(&mut stream, request, &1u64.to_le_bytes())?;
                }
                continue;
            };
            debug!("{request:?} ({size} bytes, {} fds)", fds.len());
            let answer = self.handle(request, &payload, fds);
            match answer {
                Ok(Some(body)) => reply(&mut stream, request as u32, &body)?,
                Ok(None) => {
                    if flags & FLAG_NEED_REPLY != 0
                        && self.protocol_features & PROTOCOL_F_REPLY_ACK != 0
                    {
                        reply(&mut stream, request as u32, &0u64.to_le_bytes())?;
                    }
                }
                Err(e) => {
                    error!("{request:?}: {e}");
                    if flags & FLAG_NEED_REPLY != 0 {
                        reply(&mut stream, request as u32, &1u64.to_le_bytes())?;
                    }
                }
            }
        }
    }

    fn handle(
        &mut self,
        request: Request,
        payload: &[u8],
        mut fds: Vec<OwnedFd>,
    ) -> io::Result<Option<Vec<u8>>> {
        use Request::*;
        match request {
            GetFeatures => Ok(Some(
                (VIRTIO_F_VERSION_1
                    | VIRTIO_RING_F_INDIRECT_DESC
                    | VHOST_F_LOG_ALL
                    | VHOST_USER_F_PROTOCOL_FEATURES)
                    .to_le_bytes()
                    .to_vec(),
            )),
            SetFeatures => {
                self.features.store(u64_at(payload, 0)?, Ordering::Release);
                Ok(None)
            }
            GetProtocolFeatures => Ok(Some(
                (PROTOCOL_F_MQ
                    | PROTOCOL_F_LOG_SHMFD
                    | PROTOCOL_F_REPLY_ACK
                    | PROTOCOL_F_DEVICE_STATE)
                    .to_le_bytes()
                    .to_vec(),
            )),
            // QEMU waits for this reply when LOG_SHMFD is negotiated.
            SetLogBase => {
                let fd = fds
                    .pop()
                    .ok_or_else(|| io::Error::other("SET_LOG_BASE without a descriptor"))?;
                let log = DirtyLog::map(fd, u64_at(payload, 0)?, u64_at(payload, 8)?)?;
                *self.log.lock().unwrap() = Some(Arc::new(log));
                Ok(Some(0u64.to_le_bytes().to_vec()))
            }
            // An eventfd for logging errors; this server reports none.
            SetLogFd => Ok(None),
            // Always answered: 0 in the low byte accepts, NOFD keeps QEMU's pipe.
            SetDeviceStateFd => {
                let accepted = match fds.pop() {
                    Some(pipe) => self.set_device_state_fd(payload, pipe),
                    None => Err(io::Error::other("SET_DEVICE_STATE_FD without a pipe")),
                };
                let status = match accepted {
                    Ok(()) => 0,
                    Err(e) => {
                        error!("SET_DEVICE_STATE_FD: {e}");
                        1
                    }
                };
                Ok(Some((VRING_NOFD | status).to_le_bytes().to_vec()))
            }
            CheckDeviceState => {
                let status: u64 = match self.check_device_state() {
                    Ok(()) => 0,
                    Err(e) => {
                        error!("CHECK_DEVICE_STATE: {e}");
                        1
                    }
                };
                Ok(Some(status.to_le_bytes().to_vec()))
            }
            SetProtocolFeatures => {
                self.protocol_features = u64_at(payload, 0)?;
                Ok(None)
            }
            GetQueueNum => Ok(Some((NUM_QUEUES as u64).to_le_bytes().to_vec())),
            SetOwner => Ok(None),
            ResetOwner => {
                self.vrings.iter_mut().for_each(Vring::stop);
                Ok(None)
            }
            SetMemTable => {
                self.set_mem_table(payload, fds)?;
                Ok(None)
            }
            SetVringNum => {
                let (index, num) = state(payload)?;
                self.vring(index)?.queue.lock().unwrap().size = num as u16;
                Ok(None)
            }
            SetVringAddr => {
                let index = u32_at(payload, 0)?;
                let addr = (u64_at(payload, 8)?, u64_at(payload, 16)?, u64_at(payload, 24)?);
                self.vring(index)?.addr = Some(addr);
                Ok(None)
            }
            SetVringBase => {
                let (index, num) = state(payload)?;
                self.vring(index)?.base = num as u16;
                Ok(None)
            }
            GetVringBase => {
                let (index, _) = state(payload)?;
                let vring = self.vring(index)?;
                vring.stop();
                let mut queue = vring.queue.lock().unwrap();
                let base = queue.next_avail.0;
                queue.ready = false;
                let mut body = index.to_le_bytes().to_vec();
                body.extend_from_slice(&u32::from(base).to_le_bytes());
                Ok(Some(body))
            }
            SetVringKick => {
                let value = u64_at(payload, 0)?;
                let index = (value & 0xff) as u32;
                if value & VRING_NOFD != 0 || fds.is_empty() {
                    return Err(io::Error::other("polling a ring without a kick fd is not supported"));
                }
                self.start(index, File::from(fds.remove(0)))?;
                Ok(None)
            }
            SetVringCall => {
                let value = u64_at(payload, 0)?;
                let call = if value & VRING_NOFD != 0 || fds.is_empty() {
                    None
                } else {
                    Some(File::from(fds.remove(0)))
                };
                *self.vring((value & 0xff) as u32)?.call.lock().unwrap() = call;
                Ok(None)
            }
            SetVringErr => Ok(None),
            SetVringEnable => {
                let (index, num) = state(payload)?;
                self.vring(index)?.enabled.store(num != 0, Ordering::Release);
                Ok(None)
            }
        }
    }

    fn vring(&mut self, index: u32) -> io::Result<&mut Vring> {
        self.vrings
            .get_mut(index as usize)
            .ok_or_else(|| io::Error::other(format!("no queue {index}")))
    }

    fn set_mem_table(&mut self, payload: &[u8], fds: Vec<OwnedFd>) -> io::Result<()> {
        let count = u32_at(payload, 0)? as usize;
        if count != fds.len() {
            return Err(io::Error::other(format!("{count} regions, {} fds", fds.len())));
        }
        let mut regions = Vec::with_capacity(count);
        let mut mapped = Vec::with_capacity(count);
        for (i, fd) in fds.into_iter().enumerate() {
            let at = 8 + i * 32;
            let region = MemRegion {
                guest_phys_addr: u64_at(payload, at)?,
                memory_size: u64_at(payload, at + 8)?,
                userspace_addr: u64_at(payload, at + 16)?,
            };
            let mmap_offset = u64_at(payload, at + 24)?;
            let mapping = MmapRegion::from_file(
                FileOffset::new(File::from(fd), mmap_offset),
                region.memory_size as usize,
            )
            .map_err(|e| io::Error::other(format!("mmap of region {i}: {e}")))?;
            mapped.push(
                GuestRegionMmap::new(mapping, GuestAddress(region.guest_phys_addr))
                    .ok_or_else(|| io::Error::other(format!("region {i} overflows")))?,
            );
            info!(
                "region {i}: guest {:#x}+{:#x}, offset {mmap_offset:#x}",
                region.guest_phys_addr, region.memory_size
            );
            regions.push(region);
        }
        mapped.sort_by_key(|r| r.start_addr().0);
        let mem = GuestMemoryMmap::from_regions(mapped)
            .map_err(|e| io::Error::other(format!("guest memory: {e}")))?;
        self.regions = regions;
        self.mem = Some(Arc::new(mem));
        Ok(())
    }

    /// QEMU names ring addresses in its own address space.
    fn guest_addr(&self, va: u64) -> io::Result<GuestAddress> {
        self.regions
            .iter()
            .find(|r| va >= r.userspace_addr && va - r.userspace_addr < r.memory_size)
            .map(|r| GuestAddress(r.guest_phys_addr + (va - r.userspace_addr)))
            .ok_or_else(|| io::Error::other(format!("address {va:#x} is in no region")))
    }

    fn start(&mut self, index: u32, kick: File) -> io::Result<()> {
        let mem = self
            .mem
            .clone()
            .ok_or_else(|| io::Error::other("a kick fd before the memory table"))?;
        let (desc, used, avail) = self
            .vring(index)?
            .addr
            .ok_or_else(|| io::Error::other("a kick fd before the ring addresses"))?;
        let (desc, used, avail) = (self.guest_addr(desc)?, self.guest_addr(used)?, self.guest_addr(avail)?);
        let server = self.server.clone();
        let exit_code = self.exit_code.clone();
        let vring = self.vring(index)?;
        vring.stop();
        {
            let mut queue = vring.queue.lock().unwrap();
            queue.desc_table = desc;
            queue.used_ring = used;
            queue.avail_ring = avail;
            queue.next_avail = Wrapping(vring.base);
            queue.next_used = Wrapping(vring.base);
            queue.ready = true;
            if !queue.is_valid(&mem) {
                return Err(io::Error::other(format!("queue {index} is not valid")));
            }
        }
        // Without PROTOCOL_FEATURES a ring starts enabled; with them, at SET_VRING_ENABLE.
        if self.features.load(Ordering::Acquire) & VHOST_USER_F_PROTOCOL_FEATURES == 0 {
            self.vrings[index as usize].enabled.store(true, Ordering::Release);
        }
        let vring = &mut self.vrings[index as usize];
        let (stop_rx, stop_tx) = nix::unistd::pipe().map_err(io::Error::from)?;
        let worker = Worker {
            index,
            queue: vring.queue.clone(),
            call: vring.call.clone(),
            enabled: vring.enabled.clone(),
            mem,
            server,
            exit_code,
            session: self.session.clone(),
            features: self.features.clone(),
            log: self.log.clone(),
        };
        let handle = std::thread::Builder::new()
            .name(format!("queue-{index}"))
            .spawn(move || worker.run(kick, stop_rx))?;
        vring.worker = Some((handle, stop_tx));
        info!("queue {index} started");
        Ok(())
    }
}

struct Worker<F: FileSystem + Sync + Send + 'static> {
    index: u32,
    queue: Arc<Mutex<Queue>>,
    call: Arc<Mutex<Option<File>>>,
    enabled: Arc<AtomicBool>,
    mem: Arc<GuestMemoryMmap>,
    server: Arc<Server<F>>,
    exit_code: Arc<AtomicI32>,
    session: Arc<AtomicBool>,
    features: Arc<AtomicU64>,
    log: Arc<Mutex<Option<Arc<DirtyLog>>>>,
}

impl<F: FileSystem + Sync + Send + 'static> Worker<F> {
    /// The log while VHOST_F_LOG_ALL is on.
    fn dirty_log(&self) -> Option<Arc<DirtyLog>> {
        if self.features.load(Ordering::Acquire) & VHOST_F_LOG_ALL == 0 {
            return None;
        }
        self.log.lock().unwrap().clone()
    }
    /// Runs until the write end of `stop` closes.
    fn run(self, mut kick: File, stop: OwnedFd) {
        if let Err(e) = set_nonblocking(&kick) {
            error!("queue {}: {e}", self.index);
            return;
        }
        let mut drain = [0u8; 64];
        loop {
            let mut fds = [
                PollFd::new(std::os::fd::AsFd::as_fd(&kick), PollFlags::POLLIN),
                PollFd::new(std::os::fd::AsFd::as_fd(&stop), PollFlags::POLLIN),
            ];
            match poll(&mut fds, PollTimeout::NONE) {
                Ok(_) => {}
                Err(nix::errno::Errno::EINTR) => continue,
                Err(e) => {
                    error!("queue {}: poll: {e}", self.index);
                    return;
                }
            }
            if fds[1].revents().is_some_and(|r| !r.is_empty()) {
                return;
            }
            loop {
                match kick.read(&mut drain) {
                    Ok(0) => return,
                    Ok(_) => continue,
                    Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                    Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                    Err(e) => {
                        error!("queue {}: kick: {e}", self.index);
                        return;
                    }
                }
            }
            if self.enabled.load(Ordering::Acquire) {
                self.process();
            }
        }
    }

    fn process(&self) {
        let mem = &*self.mem;
        let mut queue = self.queue.lock().unwrap();
        loop {
            if let Err(e) = queue.disable_notification(mem) {
                error!("queue {}: {e:?}", self.index);
            }
            while let Some(head) = queue.pop(mem) {
                let head_index = head.index;
                let Some(head) = head.resolve_indirect() else {
                    error!("queue {}: chain {head_index}: bad indirect table", self.index);
                    if let Err(e) = queue.add_used(mem, head_index, 0) {
                        error!("queue {}: add_used: {e:?}", self.index);
                    }
                    continue;
                };
                if log_enabled!(log::Level::Trace) {
                    let mut desc = Some(head.clone());
                    while let Some(d) = desc {
                        trace!(
                            "queue {}: desc {} addr {:#x} len {} flags {:#x}",
                            self.index, d.index, d.addr.0, d.len, d.flags
                        );
                        desc = d.next_descriptor();
                    }
                }
                self.track_session(&head);
                let log = self.dirty_log();
                // Every writable buffer, written or not: a page marked clean
                // that changed would be lost, one marked dirty only resent.
                let written: Vec<(u64, u64)> = match &log {
                    Some(_) => head
                        .clone()
                        .into_iter()
                        .writable()
                        .map(|d| (d.addr.0, u64::from(d.len)))
                        .collect(),
                    None => Vec::new(),
                };
                let len = match (Reader::new(mem, head.clone()), Writer::new(mem, head)) {
                    (Ok(reader), Ok(writer)) => {
                        debug!(
                            "queue {}: chain {head_index}: {} bytes in, {} bytes out",
                            self.index,
                            reader.available_bytes(),
                            writer.available_bytes()
                        );
                        self.server
                            .handle_message(reader, writer, false, &None, &self.exit_code, &None)
                            .unwrap_or_else(|e| {
                                error!("queue {}: {e:?}", self.index);
                                0
                            })
                    }
                    (r, w) => {
                        error!("queue {}: bad chain: {:?} {:?}", self.index, r.err(), w.err());
                        0
                    }
                };
                if let Err(e) = queue.add_used(mem, head_index, len as u32) {
                    error!("queue {}: add_used: {e:?}", self.index);
                }
                if let Some(log) = &log {
                    for (gpa, len) in written {
                        log.mark(gpa, len);
                    }
                    // flags, idx, the ring and avail_event.
                    let used_len = 6 + 8 * u64::from(queue.actual_size());
                    log.mark(queue.used_ring.0, used_len);
                }
                if queue.needs_notification(mem).unwrap_or(true) {
                    self.signal();
                }
            }
            match queue.enable_notification(mem) {
                Ok(true) => continue,
                Ok(false) => break,
                Err(e) => {
                    error!("queue {}: {e:?}", self.index);
                    break;
                }
            }
        }
    }

    fn track_session(&self, head: &DescriptorChain) {
        let Ok(mut peek) = Reader::new(&self.mem, head.clone()) else {
            return;
        };
        match peek.read_obj::<InHeader>().map(|h| h.opcode) {
            Ok(op) if op == Opcode::Init as u32 => self.session.store(true, Ordering::Release),
            Ok(op) if op == Opcode::Destroy as u32 => self.session.store(false, Ordering::Release),
            _ => {}
        }
    }

    fn signal(&self) {
        if let Some(call) = self.call.lock().unwrap().as_mut() {
            // A full pipe already holds a pending notification.
            match call.write(&1u64.to_ne_bytes()) {
                Ok(_) => {}
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {}
                Err(e) => error!("queue {}: call: {e}", self.index),
            }
        }
    }
}

fn reply(stream: &mut UnixStream, request: u32, body: &[u8]) -> io::Result<()> {
    let mut message = Vec::with_capacity(12 + body.len());
    message.extend_from_slice(&request.to_le_bytes());
    message.extend_from_slice(&(FLAG_VERSION | FLAG_REPLY).to_le_bytes());
    message.extend_from_slice(&(body.len() as u32).to_le_bytes());
    message.extend_from_slice(body);
    stream.write_all(&message)
}

fn set_nonblocking(file: &File) -> io::Result<()> {
    use nix::fcntl::{FcntlArg, OFlag, fcntl};
    let flags = OFlag::from_bits_truncate(fcntl(file, FcntlArg::F_GETFL).map_err(io::Error::from)?);
    fcntl(file, FcntlArg::F_SETFL(flags | OFlag::O_NONBLOCK)).map_err(io::Error::from)?;
    Ok(())
}

fn u32_at(payload: &[u8], at: usize) -> io::Result<u32> {
    payload
        .get(at..at + 4)
        .map(|b| u32::from_le_bytes(b.try_into().unwrap()))
        .ok_or_else(|| io::Error::other("short vhost-user payload"))
}

fn u64_at(payload: &[u8], at: usize) -> io::Result<u64> {
    payload
        .get(at..at + 8)
        .map(|b| u64::from_le_bytes(b.try_into().unwrap()))
        .ok_or_else(|| io::Error::other("short vhost-user payload"))
}

/// A `vhost_vring_state`: the ring index and a number.
fn state(payload: &[u8]) -> io::Result<(u32, u32)> {
    Ok((u32_at(payload, 0)?, u32_at(payload, 4)?))
}

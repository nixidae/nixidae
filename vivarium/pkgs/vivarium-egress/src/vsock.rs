//! A vhost-user vsock device, so a UML router needs no separate
//! vhost-device-vsock. It reads a packet from any descriptor chain:
//! Linux 7.2 splits a TX packet above about 32 KiB over several
//! descriptors, and rust-vmm's virtio-vsock 0.12 reads the payload from
//! one, so a 64 KiB write stalls there with `DescriptorLengthTooSmall`.
//!
//! Each connection is one end of a socketpair; a service thread holds
//! the other end and never sees the queues. One thread runs the queues
//! and every connection.

use std::collections::{HashMap, VecDeque};
use std::io;
use std::num::Wrapping;
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;

use vhost::vhost_user::message::{VhostUserProtocolFeatures, VhostUserVirtioFeatures};
use vhost_user_backend::{VhostUserBackend, VhostUserDaemon, VringRwLock, VringT};
use virtio_bindings::bindings::virtio_config::VIRTIO_F_VERSION_1;
use virtio_bindings::bindings::virtio_ring::VIRTIO_RING_F_EVENT_IDX;
use virtio_queue::{DescriptorChain, QueueT};
use vm_memory::{
    GuestAddressSpace, GuestMemory, GuestMemoryAtomic, GuestMemoryBackend, GuestMemoryMmap,
};
use vmm_sys_util::epoll::{ControlOperation, Epoll, EpollEvent, EventSet};
use vmm_sys_util::event::{
    EventConsumer, EventFlag, EventNotifier, new_event_consumer_and_notifier,
};
use vmm_sys_util::eventfd::{EFD_NONBLOCK, EventFd};

type Mem = GuestMemoryAtomic<GuestMemoryMmap>;

const HDR: usize = 44;
const HOST_CID: u64 = 2;
const STREAM: u16 = 1;
const OP_REQUEST: u16 = 1;
const OP_RESPONSE: u16 = 2;
const OP_RST: u16 = 3;
const OP_SHUTDOWN: u16 = 4;
const OP_RW: u16 = 5;
const OP_CREDIT_UPDATE: u16 = 6;
const OP_CREDIT_REQUEST: u16 = 7;
const SHUT_RCV: u32 = 1;
const SHUT_SEND: u32 = 2;

const RX: usize = 0;
const TX: usize = 1;
const QUEUES: usize = 3;
/// Above the queues and the library's own exit event.
const BACKEND_EVENT: u16 = 4;
const WAKE: u64 = u64::MAX;

/// What the guest may send before it hears our fwd_cnt again: the
/// guest-to-host window, as the relay's `RELAY_VSOCK_BUFFER` is the
/// other way. A full host socket spills up to this much per connection.
const BUF_ALLOC: u32 = 4 << 20;
const MAX_PKT: usize = 64 << 10;

/// A service gets the far end of each guest connection to its port.
pub type Service = Arc<dyn Fn(UnixStream) + Send + Sync>;

#[derive(Clone, Copy, Default)]
struct Header {
    src_cid: u64,
    dst_cid: u64,
    src_port: u32,
    dst_port: u32,
    len: u32,
    kind: u16,
    op: u16,
    flags: u32,
    buf_alloc: u32,
    fwd_cnt: u32,
}

impl Header {
    fn parse(b: &[u8; HDR]) -> Self {
        let u64_at = |i: usize| u64::from_le_bytes(b[i..i + 8].try_into().unwrap());
        let u32_at = |i: usize| u32::from_le_bytes(b[i..i + 4].try_into().unwrap());
        let u16_at = |i: usize| u16::from_le_bytes(b[i..i + 2].try_into().unwrap());
        Header {
            src_cid: u64_at(0),
            dst_cid: u64_at(8),
            src_port: u32_at(16),
            dst_port: u32_at(20),
            len: u32_at(24),
            kind: u16_at(28),
            op: u16_at(30),
            flags: u32_at(32),
            buf_alloc: u32_at(36),
            fwd_cnt: u32_at(40),
        }
    }

    fn bytes(&self) -> [u8; HDR] {
        let mut b = [0u8; HDR];
        b[0..8].copy_from_slice(&self.src_cid.to_le_bytes());
        b[8..16].copy_from_slice(&self.dst_cid.to_le_bytes());
        b[16..20].copy_from_slice(&self.src_port.to_le_bytes());
        b[20..24].copy_from_slice(&self.dst_port.to_le_bytes());
        b[24..28].copy_from_slice(&self.len.to_le_bytes());
        b[28..30].copy_from_slice(&self.kind.to_le_bytes());
        b[30..32].copy_from_slice(&self.op.to_le_bytes());
        b[32..36].copy_from_slice(&self.flags.to_le_bytes());
        b[36..40].copy_from_slice(&self.buf_alloc.to_le_bytes());
        b[40..44].copy_from_slice(&self.fwd_cnt.to_le_bytes());
        b
    }
}

/// (guest port, host port)
type Key = (u32, u32);

struct Conn {
    key: Key,
    stream: UnixStream,
    connected: bool,
    peer_buf_alloc: u32,
    peer_fwd_cnt: Wrapping<u32>,
    /// Bytes sent to the guest.
    tx_cnt: Wrapping<u32>,
    /// Bytes from the guest that reached the socket.
    fwd_cnt: Wrapping<u32>,
    fwd_cnt_sent: Wrapping<u32>,
    credit_queued: bool,
    spill: Vec<u8>,
    spill_at: usize,
    readable: bool,
    in_ready: bool,
    read_eof: bool,
    peer_shutdown: u32,
    write_shut: bool,
}

impl Conn {
    fn credit(&self) -> u32 {
        self.peer_buf_alloc
            .saturating_sub((self.tx_cnt - self.peer_fwd_cnt).0)
    }

    fn can_send(&self) -> bool {
        self.connected && self.readable && !self.read_eof && self.credit() > 0
    }
}

#[derive(Clone, Copy)]
struct Ctrl {
    key: Key,
    op: u16,
    flags: u32,
}

enum Read {
    Data(usize),
    Eof,
    Empty,
}

#[derive(Default)]
struct Stats {
    tx_chains: u64,
    tx_multi: u64,
    from_guest: [u64; 8],
    to_guest: [u64; 8],
    rx_bytes: u64,
    spilled: u64,
}

struct State {
    mem: Option<Mem>,
    event_idx: bool,
    guest_cid: u64,
    services: HashMap<u32, Service>,
    any: Option<Service>,
    conns: HashMap<u64, Conn>,
    by_key: HashMap<Key, u64>,
    next_id: u64,
    next_port: u32,
    ctrl: VecDeque<Ctrl>,
    ready: VecDeque<u64>,
    iov: Vec<libc::iovec>,
    payload: Vec<libc::iovec>,
    stats: Stats,
}

// SAFETY: the iovec scratch vectors hold pointers into guest memory only
// while one call runs, under the state lock.
unsafe impl Send for State {}

pub struct Device {
    state: Mutex<State>,
    epoll: Epoll,
    wake: EventFd,
}

impl Device {
    /// `services` maps a host port to its service; `any` serves the rest.
    pub fn new(
        guest_cid: u64,
        services: HashMap<u32, Service>,
        any: Option<Service>,
    ) -> io::Result<Arc<Self>> {
        let epoll = Epoll::new()?;
        let wake = EventFd::new(EFD_NONBLOCK)?;
        epoll.ctl(
            ControlOperation::Add,
            wake.as_raw_fd(),
            EpollEvent::new(EventSet::IN, WAKE),
        )?;
        Ok(Arc::new(Device {
            state: Mutex::new(State {
                mem: None,
                event_idx: false,
                guest_cid,
                services,
                any,
                conns: HashMap::new(),
                by_key: HashMap::new(),
                next_id: 0,
                next_port: 1 << 30,
                ctrl: VecDeque::new(),
                ready: VecDeque::new(),
                iov: Vec::new(),
                payload: Vec::new(),
                stats: Stats::default(),
            }),
            epoll,
            wake,
        }))
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap()
    }

    /// Serve the frontend on `socket`, one after another, forever.
    pub fn run(self: &Arc<Self>, socket: &Path) -> io::Result<()> {
        loop {
            let mut daemon = VhostUserDaemon::new(
                "egress-vsock".into(),
                self.clone(),
                GuestMemoryAtomic::new(GuestMemoryMmap::new()),
            )
            .map_err(|err| io::Error::other(format!("{err:?}")))?;
            for handler in daemon.get_epoll_handlers() {
                handler.register_listener(
                    self.epoll.as_raw_fd(),
                    EventSet::IN,
                    u64::from(BACKEND_EVENT),
                )?;
            }
            let result = daemon.serve(socket);
            let mut st = self.lock();
            eprintln!(
                "egress: vsock: frontend gone ({result:?}); tx chains {}, with more than two descriptors {}; \
                 ops from the guest {:?}, to the guest {:?} (by op 0-7); rx data bytes {}; spilled bytes {}",
                st.stats.tx_chains,
                st.stats.tx_multi,
                st.stats.from_guest,
                st.stats.to_guest,
                st.stats.rx_bytes,
                st.stats.spilled
            );
            st.reset();
        }
    }

    /// Open a connection to the guest's `port`. Bytes written before the
    /// guest answers wait in the socket.
    pub fn connect(&self, port: u32) -> io::Result<UnixStream> {
        let mut st = self.lock();
        let host_port = st.next_port;
        st.next_port = st.next_port.wrapping_add(1).max(1 << 30);
        let (ours, theirs) = UnixStream::pair()?;
        st.add_conn(&self.epoll, (port, host_port), ours, false)?;
        st.ctrl.push_back(Ctrl {
            key: (port, host_port),
            op: OP_REQUEST,
            flags: 0,
        });
        drop(st);
        self.wake.write(1)?;
        Ok(theirs)
    }
}

impl State {
    fn reset(&mut self) {
        self.conns.clear();
        self.by_key.clear();
        self.ctrl.clear();
        self.ready.clear();
        self.mem = None;
        self.stats = Stats::default();
    }

    fn add_conn(
        &mut self,
        epoll: &Epoll,
        key: Key,
        stream: UnixStream,
        connected: bool,
    ) -> io::Result<u64> {
        stream.set_nonblocking(true)?;
        let id = self.next_id;
        self.next_id += 1;
        epoll.ctl(
            ControlOperation::Add,
            stream.as_raw_fd(),
            EpollEvent::new(
                EventSet::IN | EventSet::OUT | EventSet::READ_HANG_UP | EventSet::EDGE_TRIGGERED,
                id,
            ),
        )?;
        self.conns.insert(
            id,
            Conn {
                key,
                stream,
                connected,
                peer_buf_alloc: 0,
                peer_fwd_cnt: Wrapping(0),
                tx_cnt: Wrapping(0),
                fwd_cnt: Wrapping(0),
                fwd_cnt_sent: Wrapping(0),
                credit_queued: false,
                spill: Vec::new(),
                spill_at: 0,
                readable: false,
                in_ready: false,
                read_eof: false,
                peer_shutdown: 0,
                write_shut: false,
            },
        );
        self.by_key.insert(key, id);
        Ok(id)
    }

    fn remove(&mut self, id: u64) {
        if let Some(conn) = self.conns.remove(&id) {
            self.by_key.remove(&conn.key);
        }
    }

    fn rst(&mut self, id: u64) {
        if let Some(conn) = self.conns.get(&id) {
            self.ctrl.push_back(Ctrl {
                key: conn.key,
                op: OP_RST,
                flags: 0,
            });
        }
        self.remove(id);
    }

    fn mark_ready(&mut self, id: u64) {
        if let Some(conn) = self.conns.get_mut(&id)
            && conn.can_send()
            && !conn.in_ready
        {
            conn.in_ready = true;
            self.ready.push_back(id);
        }
    }

    fn queue_credit(&mut self, id: u64) {
        if let Some(conn) = self.conns.get_mut(&id)
            && !conn.credit_queued
            && (conn.fwd_cnt - conn.fwd_cnt_sent).0 >= BUF_ALLOC / 4
        {
            conn.credit_queued = true;
            self.ctrl.push_back(Ctrl {
                key: conn.key,
                op: OP_CREDIT_UPDATE,
                flags: 0,
            });
        }
    }

    fn header(&mut self, key: Key, op: u16, flags: u32, len: u32) -> Header {
        let (buf_alloc, fwd_cnt) = match self.by_key.get(&key).and_then(|id| self.conns.get_mut(id))
        {
            Some(conn) => {
                conn.fwd_cnt_sent = conn.fwd_cnt;
                if op == OP_CREDIT_UPDATE {
                    conn.credit_queued = false;
                }
                (BUF_ALLOC, conn.fwd_cnt.0)
            }
            None => (0, 0),
        };
        self.stats.to_guest[usize::from(op.min(7))] += 1;
        self.stats.rx_bytes += u64::from(len);
        Header {
            src_cid: HOST_CID,
            dst_cid: self.guest_cid,
            src_port: key.1,
            dst_port: key.0,
            len,
            kind: STREAM,
            op,
            flags,
            buf_alloc,
            fwd_cnt,
        }
    }

    /// Spill drained: apply the guest's shutdown.
    fn settle(&mut self, id: u64) {
        let Some(conn) = self.conns.get_mut(&id) else {
            return;
        };
        if conn.spill_at < conn.spill.len() {
            return;
        }
        if conn.peer_shutdown == SHUT_RCV | SHUT_SEND {
            self.rst(id);
        } else if conn.peer_shutdown & SHUT_SEND != 0 && !conn.write_shut {
            let _ = conn.stream.shutdown(std::net::Shutdown::Write);
            conn.write_shut = true;
        }
    }

    fn flush(&mut self, id: u64) {
        let Some(conn) = self.conns.get_mut(&id) else {
            return;
        };
        while conn.spill_at < conn.spill.len() {
            let rest = &conn.spill[conn.spill_at..];
            let n =
                unsafe { libc::write(conn.stream.as_raw_fd(), rest.as_ptr().cast(), rest.len()) };
            if n < 0 {
                let err = io::Error::last_os_error();
                match err.kind() {
                    io::ErrorKind::Interrupted => continue,
                    io::ErrorKind::WouldBlock => {
                        conn.spill.drain(..conn.spill_at);
                        conn.spill_at = 0;
                        return self.queue_credit(id);
                    }
                    _ => return self.rst(id),
                }
            }
            conn.spill_at += n as usize;
            conn.fwd_cnt += n as u32;
        }
        conn.spill.clear();
        conn.spill_at = 0;
        self.queue_credit(id);
        self.settle(id);
    }

    fn write_payload(&mut self, id: u64, payload: &[libc::iovec]) {
        let total = iov_len(payload);
        let Some(conn) = self.conns.get_mut(&id) else {
            return;
        };
        if total == 0 {
            return;
        }
        let mut written = 0;
        if conn.spill_at == conn.spill.len() {
            loop {
                let n = unsafe {
                    libc::writev(
                        conn.stream.as_raw_fd(),
                        payload.as_ptr(),
                        payload.len() as i32,
                    )
                };
                if n >= 0 {
                    written = n as usize;
                    break;
                }
                let err = io::Error::last_os_error();
                match err.kind() {
                    io::ErrorKind::Interrupted => continue,
                    io::ErrorKind::WouldBlock => break,
                    _ => return self.rst(id),
                }
            }
        }
        if written < total {
            self.stats.spilled += (total - written) as u64;
            let mut rest = payload.to_vec();
            advance(&mut rest, written);
            for v in &rest {
                // SAFETY: guest memory the caller's guard keeps mapped.
                let part =
                    unsafe { std::slice::from_raw_parts(v.iov_base as *const u8, v.iov_len) };
                conn.spill.extend_from_slice(part);
            }
        }
        conn.fwd_cnt += written as u32;
        self.queue_credit(id);
    }

    fn on_packet(&mut self, epoll: &Epoll, hdr: Header, payload: &[libc::iovec]) {
        let key = (hdr.src_port, hdr.dst_port);
        self.stats.from_guest[usize::from(hdr.op.min(7))] += 1;
        let reply_rst = |st: &mut State| {
            if hdr.op != OP_RST {
                st.ctrl.push_back(Ctrl {
                    key,
                    op: OP_RST,
                    flags: 0,
                });
            }
        };
        if hdr.dst_cid != HOST_CID || hdr.kind != STREAM {
            return reply_rst(self);
        }
        let id = self.by_key.get(&key).copied();
        if let Some(id) = id
            && let Some(conn) = self.conns.get_mut(&id)
        {
            conn.peer_buf_alloc = hdr.buf_alloc;
            conn.peer_fwd_cnt = Wrapping(hdr.fwd_cnt);
        }
        match (hdr.op, id) {
            (OP_REQUEST, None) => {
                let service = self
                    .services
                    .get(&hdr.dst_port)
                    .or(self.any.as_ref())
                    .cloned();
                let Some(service) = service else {
                    return reply_rst(self);
                };
                let accepted = UnixStream::pair().and_then(|(ours, theirs)| {
                    let id = self.add_conn(epoll, key, ours, true)?;
                    Ok((id, theirs))
                });
                match accepted {
                    Ok((id, theirs)) => {
                        let conn = self.conns.get_mut(&id).unwrap();
                        conn.peer_buf_alloc = hdr.buf_alloc;
                        conn.peer_fwd_cnt = Wrapping(hdr.fwd_cnt);
                        self.ctrl.push_back(Ctrl {
                            key,
                            op: OP_RESPONSE,
                            flags: 0,
                        });
                        thread::spawn(move || service(theirs));
                    }
                    Err(err) => {
                        eprintln!("egress: vsock: accept {key:?}: {err}");
                        reply_rst(self);
                    }
                }
            }
            (OP_RESPONSE, Some(id)) => {
                let conn = self.conns.get_mut(&id).unwrap();
                if conn.connected {
                    return self.rst(id);
                }
                conn.connected = true;
                self.mark_ready(id);
            }
            (OP_RW, Some(id)) if self.conns[&id].connected => {
                self.write_payload(id, payload);
                self.mark_ready(id);
            }
            (OP_SHUTDOWN, Some(id)) => {
                self.conns.get_mut(&id).unwrap().peer_shutdown |=
                    hdr.flags & (SHUT_RCV | SHUT_SEND);
                self.settle(id);
            }
            (OP_RST, Some(id)) => self.remove(id),
            (OP_CREDIT_UPDATE, Some(id)) => self.mark_ready(id),
            (OP_CREDIT_REQUEST, Some(id)) => {
                let key = self.conns[&id].key;
                self.ctrl.push_back(Ctrl {
                    key,
                    op: OP_CREDIT_UPDATE,
                    flags: 0,
                });
            }
            (_, Some(id)) => self.rst(id),
            (_, None) => reply_rst(self),
        }
    }

    fn process_tx(&mut self, epoll: &Epoll, vring: &VringRwLock) -> io::Result<()> {
        let Some(atomic) = self.mem.clone() else {
            return Ok(());
        };
        if !vring.get_ref().get_queue().ready() {
            return Ok(());
        }
        let mut used = false;
        loop {
            if self.event_idx {
                vring.disable_notification().map_err(io::Error::other)?;
            }
            loop {
                let mem = atomic.memory();
                let chain = vring
                    .get_mut()
                    .get_queue_mut()
                    .pop_descriptor_chain(mem.clone());
                let Some(chain) = chain else {
                    break;
                };
                let head = chain.head_index();
                let mut iov = std::mem::take(&mut self.iov);
                iov.clear();
                let descriptors = gather(&mem, chain, false, &mut iov);
                self.stats.tx_chains += 1;
                if descriptors > 2 {
                    self.stats.tx_multi += 1;
                }
                let mut raw = [0u8; HDR];
                if copy_out(&iov, &mut raw) == HDR {
                    let hdr = Header::parse(&raw);
                    advance(&mut iov, HDR);
                    truncate(&mut iov, hdr.len as usize);
                    self.on_packet(epoll, hdr, &iov);
                }
                self.iov = iov;
                vring.add_used(head, 0).map_err(io::Error::other)?;
                used = true;
            }
            if !self.event_idx || !vring.enable_notification().map_err(io::Error::other)? {
                break;
            }
        }
        if used && vring.needs_notification().map_err(io::Error::other)? {
            vring.signal_used_queue()?;
        }
        Ok(())
    }

    fn read_into(&mut self, id: u64, room: usize) -> io::Result<Read> {
        let conn = self.conns.get_mut(&id).unwrap();
        let max = room.min(conn.credit() as usize).min(MAX_PKT);
        truncate(&mut self.payload, max);
        loop {
            let n = unsafe {
                libc::readv(
                    conn.stream.as_raw_fd(),
                    self.payload.as_ptr(),
                    self.payload.len() as i32,
                )
            };
            if n > 0 {
                conn.tx_cnt += n as u32;
                return Ok(Read::Data(n as usize));
            }
            if n == 0 {
                return Ok(Read::Eof);
            }
            let err = io::Error::last_os_error();
            match err.kind() {
                io::ErrorKind::Interrupted => continue,
                io::ErrorKind::WouldBlock => return Ok(Read::Empty),
                _ => return Err(err),
            }
        }
    }

    fn process_rx(&mut self, vring: &VringRwLock) -> io::Result<()> {
        let Some(atomic) = self.mem.clone() else {
            return Ok(());
        };
        if !vring.get_ref().get_queue().ready() {
            return Ok(());
        }
        let mut used = false;
        loop {
            // Skip connections that went away or lost credit.
            while let Some(&id) = self.ready.front() {
                match self.conns.get_mut(&id) {
                    Some(conn) if conn.can_send() => break,
                    Some(conn) => {
                        conn.in_ready = false;
                        self.ready.pop_front();
                    }
                    None => {
                        self.ready.pop_front();
                    }
                }
            }
            if self.ctrl.is_empty() && self.ready.is_empty() {
                break;
            }
            let mem = atomic.memory();
            let (saved, chain) = {
                let mut state = vring.get_mut();
                let queue = state.get_queue_mut();
                (queue.next_avail(), queue.pop_descriptor_chain(mem.clone()))
            };
            let Some(chain) = chain else {
                if self.event_idx && vring.enable_notification().map_err(io::Error::other)? {
                    continue;
                }
                break;
            };
            let head = chain.head_index();
            let mut iov = std::mem::take(&mut self.iov);
            iov.clear();
            gather(&mem, chain, true, &mut iov);
            let room = iov_len(&iov).saturating_sub(HDR);
            // A zero-length readv reads as end of file.
            if iov_len(&iov) < HDR || (room == 0 && self.ctrl.is_empty()) {
                self.iov = iov;
                vring.add_used(head, 0).map_err(io::Error::other)?;
                used = true;
                continue;
            }
            let len = if let Some(ctrl) = self.ctrl.pop_front() {
                let hdr = self.header(ctrl.key, ctrl.op, ctrl.flags, 0);
                copy_in(&iov, &hdr.bytes());
                Some(HDR)
            } else {
                let id = *self.ready.front().unwrap();
                self.payload.clear();
                self.payload.extend_from_slice(&iov);
                advance(&mut self.payload, HDR);
                let key = self.conns[&id].key;
                match self.read_into(id, room) {
                    Ok(Read::Data(n)) => {
                        let hdr = self.header(key, OP_RW, 0, n as u32);
                        copy_in(&iov, &hdr.bytes());
                        self.ready.rotate_left(1);
                        Some(HDR + n)
                    }
                    Ok(Read::Eof) => {
                        let conn = self.conns.get_mut(&id).unwrap();
                        conn.read_eof = true;
                        let hdr = self.header(key, OP_SHUTDOWN, SHUT_SEND, 0);
                        copy_in(&iov, &hdr.bytes());
                        Some(HDR)
                    }
                    Ok(Read::Empty) => {
                        self.conns.get_mut(&id).unwrap().readable = false;
                        None
                    }
                    Err(_) => {
                        self.remove(id);
                        let hdr = self.header(key, OP_RST, 0, 0);
                        copy_in(&iov, &hdr.bytes());
                        Some(HDR)
                    }
                }
            };
            self.iov = iov;
            match len {
                Some(len) => {
                    vring.add_used(head, len as u32).map_err(io::Error::other)?;
                    used = true;
                }
                None => vring.set_queue_next_avail(saved),
            }
        }
        if used && vring.needs_notification().map_err(io::Error::other)? {
            vring.signal_used_queue()?;
        }
        Ok(())
    }

    fn on_socket(&mut self, id: u64, set: EventSet) {
        let Some(conn) = self.conns.get_mut(&id) else {
            return;
        };
        if set
            .intersects(EventSet::IN | EventSet::READ_HANG_UP | EventSet::HANG_UP | EventSet::ERROR)
        {
            conn.readable = true;
            self.mark_ready(id);
        }
        if set.intersects(EventSet::OUT | EventSet::ERROR) {
            self.flush(id);
        }
    }
}

impl VhostUserBackend for Device {
    type Bitmap = ();
    type Vring = VringRwLock;

    fn num_queues(&self) -> usize {
        QUEUES
    }

    fn max_queue_size(&self) -> usize {
        256
    }

    fn features(&self) -> u64 {
        (1 << VIRTIO_F_VERSION_1)
            | (1 << VIRTIO_RING_F_EVENT_IDX)
            | VhostUserVirtioFeatures::PROTOCOL_FEATURES.bits()
    }

    fn protocol_features(&self) -> VhostUserProtocolFeatures {
        VhostUserProtocolFeatures::MQ | VhostUserProtocolFeatures::CONFIG
    }

    fn set_event_idx(&self, enabled: bool) {
        self.lock().event_idx = enabled;
    }

    fn update_memory(&self, mem: Mem) -> io::Result<()> {
        self.lock().mem = Some(mem);
        Ok(())
    }

    /// A fresh pair for each frontend: `serve` signals it when the
    /// frontend goes, and the worker thread and its vrings end with it.
    fn exit_event(&self, _thread_index: usize) -> Option<(EventConsumer, EventNotifier)> {
        new_event_consumer_and_notifier(EventFlag::NONBLOCK).ok()
    }

    fn get_config(&self, offset: u32, size: u32) -> Vec<u8> {
        let config = self.lock().guest_cid.to_le_bytes();
        let (offset, size) = (offset as usize, size as usize);
        config
            .get(offset..offset + size)
            .map(<[u8]>::to_vec)
            .unwrap_or_default()
    }

    fn handle_event(
        &self,
        device_event: u16,
        _evset: EventSet,
        vrings: &[VringRwLock],
        _thread_id: usize,
    ) -> io::Result<()> {
        let mut st = self.lock();
        match device_event {
            0 => {}
            1 => st.process_tx(&self.epoll, &vrings[TX])?,
            BACKEND_EVENT => {
                let mut events = [EpollEvent::default(); 64];
                loop {
                    let n = self.epoll.wait(0, &mut events)?;
                    for event in &events[..n] {
                        if event.data() == WAKE {
                            let _ = self.wake.read();
                        } else {
                            st.on_socket(event.data(), event.event_set());
                        }
                    }
                    if n < events.len() {
                        break;
                    }
                }
            }
            _ => return Ok(()),
        }
        st.process_rx(&vrings[RX])
    }
}

/// The descriptors of a chain as iovecs: the readable ones for TX, the
/// writable ones for RX. Returns how many descriptors it took.
fn gather<M>(
    mem: &GuestMemoryMmap,
    chain: DescriptorChain<M>,
    writable: bool,
    out: &mut Vec<libc::iovec>,
) -> usize
where
    M: std::ops::Deref,
    M::Target: GuestMemory,
{
    let mut count = 0;
    for desc in chain {
        if desc.is_write_only() != writable {
            continue;
        }
        count += 1;
        for slice in GuestMemoryBackend::get_slices(mem, desc.addr(), desc.len() as usize) {
            let Ok(slice) = slice else {
                break;
            };
            out.push(libc::iovec {
                iov_base: slice.ptr_guard_mut().as_ptr().cast(),
                iov_len: slice.len(),
            });
        }
    }
    count
}

fn iov_len(iov: &[libc::iovec]) -> usize {
    iov.iter().map(|v| v.iov_len).sum()
}

fn advance(iov: &mut Vec<libc::iovec>, mut n: usize) {
    let mut drop = 0;
    for v in iov.iter_mut() {
        if n == 0 {
            break;
        }
        let step = n.min(v.iov_len);
        v.iov_base = unsafe { v.iov_base.cast::<u8>().add(step).cast() };
        v.iov_len -= step;
        n -= step;
        if v.iov_len == 0 {
            drop += 1;
        }
    }
    iov.drain(..drop);
}

fn truncate(iov: &mut Vec<libc::iovec>, mut n: usize) {
    let mut keep = 0;
    for v in iov.iter_mut() {
        if n == 0 {
            break;
        }
        v.iov_len = v.iov_len.min(n);
        n -= v.iov_len;
        keep += 1;
    }
    iov.truncate(keep);
}

fn copy_out(iov: &[libc::iovec], buf: &mut [u8]) -> usize {
    let mut at = 0;
    for v in iov {
        if at == buf.len() {
            break;
        }
        let step = v.iov_len.min(buf.len() - at);
        unsafe {
            std::ptr::copy_nonoverlapping(v.iov_base as *const u8, buf[at..].as_mut_ptr(), step)
        };
        at += step;
    }
    at
}

fn copy_in(iov: &[libc::iovec], data: &[u8]) {
    let mut at = 0;
    for v in iov {
        if at == data.len() {
            break;
        }
        let step = v.iov_len.min(data.len() - at);
        unsafe {
            std::ptr::copy_nonoverlapping(data[at..].as_ptr(), v.iov_base.cast::<u8>(), step)
        };
        at += step;
    }
}

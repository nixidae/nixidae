//! What the relay and the egress share: the request that opens a flow,
//! and the copy loop.

use std::io::{self, Read, Write};
use std::mem;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, Shutdown, SocketAddr, UdpSocket};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(all(target_os = "linux", feature = "vhost-user"))]
pub mod vsock;

pub const ICMP: u8 = 1;
pub const TCP: u8 = 6;
pub const UDP: u8 = 17;
pub const REQUEST_LEN: usize = 20;
/// A UDP or ICMP flow ends after this long with no datagram either way.
pub const IDLE: Duration = Duration::from_secs(30);
/// How often a blocked datagram reader looks at the idle clock.
pub const TICK: Duration = Duration::from_secs(2);
/// Large enough that a copy is a few system calls per MiB: in a UML
/// guest every system call traps to the host.
pub const BUFFER: usize = 256 * 1024;
/// The copy loop yields the CPU before each read. On a uniprocessor
/// guest the reader's wakeup preempts the kernel thread that queues the
/// data; yielding lets it queue the rest, so one read takes many packets.
pub static YIELD_BEFORE_READ: AtomicBool = AtomicBool::new(false);
const SHORT_READ: usize = 64 * 1024;
const UNIX_BUFFER: libc::c_int = 1024 * 1024;

/// `proto (1) | family (1) | address (16) | port (2, big-endian)`.
pub fn encode(proto: u8, dst: SocketAddr) -> [u8; REQUEST_LEN] {
    let mut out = [0u8; REQUEST_LEN];
    out[0] = proto;
    match dst.ip() {
        IpAddr::V4(ip) => {
            out[1] = 4;
            out[2..6].copy_from_slice(&ip.octets());
        }
        IpAddr::V6(ip) => {
            out[1] = 6;
            out[2..18].copy_from_slice(&ip.octets());
        }
    }
    out[18..20].copy_from_slice(&dst.port().to_be_bytes());
    out
}

pub fn decode(raw: &[u8; REQUEST_LEN]) -> io::Result<(u8, SocketAddr)> {
    let port = u16::from_be_bytes([raw[18], raw[19]]);
    let ip = match raw[1] {
        4 => IpAddr::V4(Ipv4Addr::new(raw[2], raw[3], raw[4], raw[5])),
        6 => {
            let mut octets = [0u8; 16];
            octets.copy_from_slice(&raw[2..18]);
            IpAddr::V6(Ipv6Addr::from(octets))
        }
        family => return Err(io::Error::other(format!("unknown address family {family}"))),
    };
    Ok((raw[0], SocketAddr::new(ip, port)))
}

/// A connected stream socket of any family, for copying.
pub struct Stream(OwnedFd);

impl Stream {
    pub fn new(fd: OwnedFd) -> Self {
        Self(fd)
    }

    pub fn try_clone(&self) -> io::Result<Self> {
        Ok(Self(self.0.try_clone()?))
    }

    pub fn set_read_timeout(&self, timeout: Duration) -> io::Result<()> {
        let tv = libc::timeval {
            tv_sec: timeout.as_secs() as libc::time_t,
            tv_usec: timeout.subsec_micros() as _,
        };
        setsockopt(self.0.as_raw_fd(), libc::SOL_SOCKET, libc::SO_RCVTIMEO, &tv)
    }

    /// One datagram as `length (2, big-endian) | bytes`.
    pub fn read_frame(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let mut len = [0u8; 2];
        self.read_exact(&mut len)?;
        let len = u16::from_be_bytes(len) as usize;
        self.read_exact(&mut buf[..len])?;
        Ok(len)
    }

    /// Drops the datagram when the stream is full, as a congested link
    /// would. A frame half sent is finished, so the framing holds.
    pub fn write_frame_or_drop(&mut self, data: &[u8]) -> io::Result<bool> {
        let mut frame = Vec::with_capacity(data.len() + 2);
        frame.extend_from_slice(&(data.len() as u16).to_be_bytes());
        frame.extend_from_slice(data);
        let n = unsafe {
            libc::send(
                self.0.as_raw_fd(),
                frame.as_ptr().cast(),
                frame.len(),
                libc::MSG_DONTWAIT | libc::MSG_NOSIGNAL,
            )
        };
        if n < 0 {
            let err = io::Error::last_os_error();
            return if err.kind() == io::ErrorKind::WouldBlock {
                Ok(false)
            } else {
                Err(err)
            };
        }
        self.write_all(&frame[n as usize..])?;
        Ok(true)
    }

    pub fn shutdown(&self, how: Shutdown) {
        let how = match how {
            Shutdown::Read => libc::SHUT_RD,
            Shutdown::Write => libc::SHUT_WR,
            Shutdown::Both => libc::SHUT_RDWR,
        };
        // The peer may be gone already; the copy has ended either way.
        unsafe { libc::shutdown(self.0.as_raw_fd(), how) };
    }
}

impl Read for Stream {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let n = unsafe { libc::read(self.0.as_raw_fd(), buf.as_mut_ptr().cast(), buf.len()) };
        if n < 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(n as usize)
        }
    }
}

impl Write for Stream {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let n = unsafe { libc::write(self.0.as_raw_fd(), buf.as_ptr().cast(), buf.len()) };
        if n < 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(n as usize)
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl<T: Into<OwnedFd>> From<T> for Stream {
    fn from(value: T) -> Self {
        Self(value.into())
    }
}

/// Copy both ways until both sides have closed, passing on each
/// half-close.
pub fn splice_both(a: Stream, b: Stream) -> io::Result<()> {
    let (a2, b2) = (a.try_clone()?, b.try_clone()?);
    let back = thread::spawn(move || one_way(b2, a2));
    one_way(a, b);
    let _ = back.join();
    Ok(())
}

fn one_way(mut from: Stream, mut to: Stream) {
    let mut buf = vec![0u8; BUFFER];
    let yields = YIELD_BEFORE_READ.load(Ordering::Relaxed);
    let mut short = false;
    loop {
        // Only after a short read: a yield before every read cost one
        // flow into a UML guest 15-30%.
        if yields && short {
            unsafe { libc::sched_yield() };
        }
        match from.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                short = n < SHORT_READ;
                if to.write_all(&buf[..n]).is_err() {
                    break;
                }
            }
        }
    }
    to.shutdown(Shutdown::Write);
}

/// macOS gives an AF_UNIX stream 8 KiB each way
/// (`net.local.stream.sendspace`), and the sender's buffer bounds the
/// flow: a socketpair moves 14.5 Gbit/s, and 208.6 with 1 MiB on the
/// sender. libkrun widens its end the same way. Linux starts at about
/// 208 KiB and keeps its default.
pub fn widen_unix(socket: &impl AsRawFd) -> io::Result<()> {
    if cfg!(target_os = "macos") {
        for name in [libc::SO_SNDBUF, libc::SO_RCVBUF] {
            setsockopt(socket.as_raw_fd(), libc::SOL_SOCKET, name, &UNIX_BUFFER)?;
        }
    }
    Ok(())
}

pub fn setsockopt<T>(fd: i32, level: i32, name: i32, value: &T) -> io::Result<()> {
    let rc = unsafe {
        libc::setsockopt(
            fd,
            level,
            name,
            (value as *const T).cast(),
            mem::size_of::<T>() as libc::socklen_t,
        )
    };
    if rc != 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

/// Datagrams both ways between a connected datagram socket and a stream
/// of frames, until the stream closes or the flow is idle for `IDLE`.
pub fn pump(stream: Stream, socket: UdpSocket) -> io::Result<()> {
    pump_map(stream, socket, |datagram| Some(datagram))
}

/// What a ping socket receives, as Linux gives it: the ICMP echo reply
/// alone. macOS puts the IPv4 header in front, and on `::1` also hands
/// back the echo request this socket sent.
pub fn echo_reply(packet: &[u8]) -> Option<&[u8]> {
    let icmp = match packet.first() {
        Some(byte) if byte >> 4 == 4 => packet.get(usize::from(byte & 0x0f) * 4..)?,
        _ => packet,
    };
    // Echo reply: 0 for ICMP, 129 for ICMPv6.
    matches!(icmp.first(), Some(0 | 129)).then_some(icmp)
}

/// `pump`, with each datagram from the socket passed through `map`; None
/// drops it.
pub fn pump_map(
    stream: Stream,
    socket: UdpSocket,
    map: fn(&[u8]) -> Option<&[u8]>,
) -> io::Result<()> {
    let activity = Arc::new(Activity::default());
    let done = Arc::new(AtomicBool::new(false));
    stream.set_read_timeout(TICK)?;
    socket.set_read_timeout(Some(TICK))?;
    let up = {
        let (mut stream, socket) = (stream.try_clone()?, socket.try_clone()?);
        let (activity, done) = (activity.clone(), done.clone());
        thread::spawn(move || {
            let mut buf = vec![0u8; 65536];
            while !done.load(Ordering::Relaxed) {
                match socket.recv(&mut buf) {
                    Ok(n) => {
                        activity.touch();
                        let Some(datagram) = map(&buf[..n]) else {
                            continue;
                        };
                        if stream.write_frame_or_drop(datagram).is_err() {
                            break;
                        }
                    }
                    Err(err) if timed_out(&err) && !activity.idle() => {}
                    // ECONNREFUSED is an ICMP error from the far end: UDP goes on.
                    Err(err) if err.kind() == io::ErrorKind::ConnectionRefused => {}
                    Err(_) => break,
                }
            }
            done.store(true, Ordering::Relaxed);
        })
    };
    let mut stream = stream;
    let mut buf = vec![0u8; 65536];
    while !done.load(Ordering::Relaxed) {
        match stream.read_frame(&mut buf) {
            Ok(n) => {
                activity.touch();
                // A refused or unroutable datagram is lost, as on a wire.
                let _ = socket.send(&buf[..n]);
            }
            Err(err) if timed_out(&err) && !activity.idle() => {}
            Err(_) => break,
        }
    }
    done.store(true, Ordering::Relaxed);
    let _ = up.join();
    stream.shutdown(Shutdown::Both);
    Ok(())
}

/// The last time a datagram crossed a flow, either way.
pub struct Activity {
    start: Instant,
    last_ms: AtomicU64,
}

impl Default for Activity {
    fn default() -> Self {
        Self {
            start: Instant::now(),
            last_ms: AtomicU64::new(0),
        }
    }
}

impl Activity {
    pub fn touch(&self) {
        self.last_ms
            .store(self.start.elapsed().as_millis() as u64, Ordering::Relaxed);
    }

    pub fn idle(&self) -> bool {
        let last = Duration::from_millis(self.last_ms.load(Ordering::Relaxed));
        self.start.elapsed().saturating_sub(last) > IDLE
    }
}

/// A read that timed out: the reader looks at the idle clock and goes on.
pub fn timed_out(err: &io::Error) -> bool {
    matches!(
        err.kind(),
        io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
    )
}

/// The Internet checksum over `data`.
pub fn checksum(data: &[u8]) -> u16 {
    let mut sum: u32 = 0;
    for pair in data.chunks(2) {
        let low = if pair.len() == 2 { pair[1] } else { 0 };
        sum += u32::from(u16::from_be_bytes([pair[0], low]));
    }
    while sum >> 16 != 0 {
        sum = (sum & 0xffff) + (sum >> 16);
    }
    !(sum as u16)
}

pub fn family(addr: SocketAddr) -> libc::c_int {
    match addr {
        SocketAddr::V4(_) => libc::AF_INET,
        SocketAddr::V6(_) => libc::AF_INET6,
    }
}

/// `addr` as the kernel takes it, and its length.
pub fn sockaddr(addr: SocketAddr) -> (libc::sockaddr_storage, libc::socklen_t) {
    let mut out: libc::sockaddr_storage = unsafe { mem::zeroed() };
    let len = match addr {
        SocketAddr::V4(addr) => {
            let sin = unsafe { &mut *(&raw mut out).cast::<libc::sockaddr_in>() };
            sin.sin_family = libc::AF_INET as libc::sa_family_t;
            sin.sin_port = addr.port().to_be();
            sin.sin_addr.s_addr = u32::from_ne_bytes(addr.ip().octets());
            mem::size_of::<libc::sockaddr_in>()
        }
        SocketAddr::V6(addr) => {
            let sin6 = unsafe { &mut *(&raw mut out).cast::<libc::sockaddr_in6>() };
            sin6.sin6_family = libc::AF_INET6 as libc::sa_family_t;
            sin6.sin6_port = addr.port().to_be();
            sin6.sin6_addr.s6_addr = addr.ip().octets();
            sin6.sin6_scope_id = addr.scope_id();
            mem::size_of::<libc::sockaddr_in6>()
        }
    };
    (out, len as libc::socklen_t)
}

/// # Safety
/// `raw` must point at a `sockaddr_in` or a `sockaddr_in6`, as its family says.
pub unsafe fn from_sockaddr(raw: *const libc::sockaddr) -> io::Result<SocketAddr> {
    match libc::c_int::from(unsafe { (*raw).sa_family }) {
        libc::AF_INET => {
            let sin = unsafe { raw.cast::<libc::sockaddr_in>().read_unaligned() };
            Ok(SocketAddr::new(
                IpAddr::V4(Ipv4Addr::from(sin.sin_addr.s_addr.to_ne_bytes())),
                u16::from_be(sin.sin_port),
            ))
        }
        libc::AF_INET6 => {
            let sin6 = unsafe { raw.cast::<libc::sockaddr_in6>().read_unaligned() };
            Ok(SocketAddr::V6(std::net::SocketAddrV6::new(
                Ipv6Addr::from(sin6.sin6_addr.s6_addr),
                u16::from_be(sin6.sin6_port),
                0,
                sin6.sin6_scope_id,
            )))
        }
        family => Err(io::Error::other(format!("address family {family}"))),
    }
}

pub fn bind(fd: &OwnedFd, addr: SocketAddr) -> io::Result<()> {
    let (sa, len) = sockaddr(addr);
    if unsafe { libc::bind(fd.as_raw_fd(), (&raw const sa).cast(), len) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

pub fn connect(fd: &OwnedFd, addr: SocketAddr) -> io::Result<()> {
    let (sa, len) = sockaddr(addr);
    if unsafe { libc::connect(fd.as_raw_fd(), (&raw const sa).cast(), len) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// A socket closed on exec. macOS has no `SOCK_CLOEXEC`, so there the
/// flag comes after: the egress starts no program.
pub fn socket(domain: i32, kind: i32, proto: i32) -> io::Result<OwnedFd> {
    #[cfg(target_os = "linux")]
    let fd = unsafe { owned(libc::socket(domain, kind | libc::SOCK_CLOEXEC, proto))? };
    #[cfg(not(target_os = "linux"))]
    let fd = {
        let fd = unsafe { owned(libc::socket(domain, kind, proto))? };
        if unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_SETFD, libc::FD_CLOEXEC) } != 0 {
            return Err(io::Error::last_os_error());
        }
        fd
    };
    Ok(fd)
}

/// # Safety
/// `fd` must be a socket this process owns and nothing else closes.
pub unsafe fn owned(fd: i32) -> io::Result<OwnedFd> {
    if fd < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(unsafe { OwnedFd::from_raw_fd(fd) })
    }
}

#[cfg(test)]
mod tests {
    use super::{REQUEST_LEN, TCP, UDP, decode, echo_reply, encode};
    use std::net::SocketAddr;

    #[test]
    fn a_request_decodes_to_what_was_encoded() {
        for (proto, dst) in [(TCP, "10.0.2.2:53"), (UDP, "[fd00:10:0:2::2]:65535")] {
            let dst: SocketAddr = dst.parse().unwrap();
            assert_eq!(decode(&encode(proto, dst)).unwrap(), (proto, dst));
        }
    }

    #[test]
    fn an_unknown_family_is_refused() {
        let mut raw = [0u8; REQUEST_LEN];
        raw[1] = 5;
        assert!(decode(&raw).is_err());
    }

    // Recorded from a ping socket to 127.0.0.1 and ::1 on macOS 27.
    const REPLY_ICMP: [u8; 13] = *b"\x00\x00\xa9\xf8\x12\x34\x00\x01hello";

    #[test]
    fn a_linux_reply_passes_unchanged() {
        assert_eq!(echo_reply(&REPLY_ICMP), Some(&REPLY_ICMP[..]));
    }

    #[test]
    fn a_macos_reply_loses_its_ipv4_header() {
        let mut packet =
            b"\x45\x00\x0d\x00\x96\xe9\x00\x00\x40\x01\x00\x00\x7f\x00\x00\x01\x7f\x00\x00\x01"
                .to_vec();
        packet.extend_from_slice(&REPLY_ICMP);
        assert_eq!(echo_reply(&packet), Some(&REPLY_ICMP[..]));
    }

    #[test]
    fn an_echo_request_is_dropped() {
        let request = b"\x80\x00\x29\xaf\x12\x34\x00\x01hello";
        assert_eq!(echo_reply(request), None);
        let reply = b"\x81\x00\x28\xaf\x12\x34\x00\x01hello";
        assert_eq!(echo_reply(reply), Some(&reply[..]));
    }
}

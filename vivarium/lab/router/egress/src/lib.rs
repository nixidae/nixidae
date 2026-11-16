//! What the relay and the egress share: the request that opens a flow,
//! and the copy loop.

use std::io::{self, Read, Write};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, Shutdown, SocketAddr};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::thread;

pub const TCP: u8 = 6;
pub const REQUEST_LEN: usize = 20;
/// Large enough that a copy is a few system calls per MiB: in a UML
/// guest every system call traps to the host.
pub const BUFFER: usize = 256 * 1024;

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
        if n < 0 { Err(io::Error::last_os_error()) } else { Ok(n as usize) }
    }
}

impl Write for Stream {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let n = unsafe { libc::write(self.0.as_raw_fd(), buf.as_ptr().cast(), buf.len()) };
        if n < 0 { Err(io::Error::last_os_error()) } else { Ok(n as usize) }
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
    loop {
        match from.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                if to.write_all(&buf[..n]).is_err() {
                    break;
                }
            }
        }
    }
    to.shutdown(Shutdown::Write);
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

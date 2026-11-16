//! In the router guest. nftables redirects every forwarded TCP flow to
//! the listen port; conntrack keeps the original destination, which
//! `SO_ORIGINAL_DST` reads back. Each flow becomes one vsock stream to
//! the host's egress, which opens the real socket.
//!
//!     relay LISTEN_PORT VSOCK_PORT

use std::env;
use std::io::{self, Read, Write};
use std::mem;
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4, TcpListener};
use std::os::fd::AsRawFd;
use std::thread;

use vivarium_egress::{Stream, TCP, encode, owned, splice_both};

const SO_ORIGINAL_DST: libc::c_int = 80;
const HOST_CID: u32 = 2;

fn original_dst(fd: i32) -> io::Result<SocketAddr> {
    let mut addr: libc::sockaddr_in = unsafe { mem::zeroed() };
    let mut len = mem::size_of::<libc::sockaddr_in>() as libc::socklen_t;
    let rc = unsafe {
        libc::getsockopt(fd, libc::SOL_IP, SO_ORIGINAL_DST, (&raw mut addr).cast(), &raw mut len)
    };
    if rc != 0 {
        return Err(io::Error::last_os_error());
    }
    let ip = Ipv4Addr::from(u32::from_be(addr.sin_addr.s_addr));
    Ok(SocketAddr::V4(SocketAddrV4::new(ip, u16::from_be(addr.sin_port))))
}

fn vsock_connect(port: u32) -> io::Result<Stream> {
    let fd = unsafe { owned(libc::socket(libc::AF_VSOCK, libc::SOCK_STREAM | libc::SOCK_CLOEXEC, 0))? };
    let mut addr: libc::sockaddr_vm = unsafe { mem::zeroed() };
    addr.svm_family = libc::AF_VSOCK as libc::sa_family_t;
    addr.svm_cid = HOST_CID;
    addr.svm_port = port;
    let rc = unsafe {
        libc::connect(
            fd.as_raw_fd(),
            (&raw const addr).cast(),
            mem::size_of::<libc::sockaddr_vm>() as libc::socklen_t,
        )
    };
    if rc != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(Stream::new(fd))
}

fn flow(guest: std::net::TcpStream, vsock_port: u32) -> io::Result<()> {
    let dst = original_dst(guest.as_raw_fd())?;
    let mut host = vsock_connect(vsock_port)?;
    host.write_all(&encode(TCP, dst))?;
    let mut status = [0u8; 1];
    host.read_exact(&mut status)?;
    if status[0] != 0 {
        // The egress could not connect: the guest sees a reset, as it
        // would from a host that refused.
        let linger = libc::linger { l_onoff: 1, l_linger: 0 };
        unsafe {
            libc::setsockopt(
                guest.as_raw_fd(),
                libc::SOL_SOCKET,
                libc::SO_LINGER,
                (&raw const linger).cast(),
                mem::size_of::<libc::linger>() as libc::socklen_t,
            )
        };
        return Ok(());
    }
    splice_both(Stream::from(guest), host)
}

fn main() -> io::Result<()> {
    let args: Vec<String> = env::args().collect();
    let listen: u16 = args.get(1).map_or(Ok(9000), |a| a.parse()).map_err(io::Error::other)?;
    let vsock_port: u32 = args.get(2).map_or(Ok(5000), |a| a.parse()).map_err(io::Error::other)?;
    let listener = TcpListener::bind(("0.0.0.0", listen))?;
    eprintln!("relay: tcp {listen} to vsock {HOST_CID}:{vsock_port}");
    for guest in listener.incoming() {
        let guest = guest?;
        thread::spawn(move || {
            if let Err(err) = flow(guest, vsock_port) {
                eprintln!("relay: {err}");
            }
        });
    }
    Ok(())
}

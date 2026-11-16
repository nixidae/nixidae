//! In the router guest. Each flow a guest sends through the router
//! becomes one vsock stream to the host's egress, which opens the real
//! socket.
//!
//! - TCP: nftables `redirect`s it to the TCP port. conntrack keeps the
//!   original destination, which `SO_ORIGINAL_DST` reads back.
//! - UDP: nftables `tproxy`s it to the UDP port, with no NAT, so no
//!   conntrack entry holds the flow: the relay keeps its own table, and
//!   a flow ends after `IDLE` with no datagram. `IP_ORIGDSTADDR` names the
//!   destination. Each flow gets a transparent socket bound to that
//!   destination and connected to the guest; the guest's later datagrams
//!   reach that socket, and replies leave from it.
//! - ICMP echo: a fwmark and a `local` route deliver it to the router,
//!   which does not answer it (`icmp_echo_ignore_all`). A raw socket reads
//!   it; the host's ping socket picks its own echo id, so the reply gets
//!   the guest's id back before it goes out on a raw socket.
//!
//! - Forwards: the host connects in on vsock IN_PORT and names a guest
//!   address; the relay connects to it.
//!
//!     relay TCP_PORT VSOCK_PORT UDP_PORT IN_PORT

use std::collections::HashMap;
use std::collections::HashSet;
use std::env;
use std::io::{self, Read, Write};
use std::mem;
use std::net::{Ipv4Addr, Shutdown, SocketAddr, SocketAddrV4, TcpListener, UdpSocket};
use std::os::fd::{AsRawFd, OwnedFd};
use std::sync::{Arc, Mutex};
use std::thread;

use vivarium_egress::{
    Activity, ICMP, REQUEST_LEN, Stream, TCP, TICK, UDP, checksum, decode, encode, owned, pump,
    setsockopt, splice_both, timed_out,
};

const SO_ORIGINAL_DST: libc::c_int = 80;
/// `IP_ORIGDSTADDR`, the control message `IP_RECVORIGDSTADDR` asks for.
const IP_ORIGDSTADDR: libc::c_int = 20;
const HOST_CID: u32 = 2;
/// `SO_VM_SOCKETS_BUFFER_SIZE` and `_MAX_SIZE`, level `AF_VSOCK`, a u64.
const SO_VM_SOCKETS_BUFFER_SIZE: libc::c_int = 0;
const SO_VM_SOCKETS_BUFFER_MAX_SIZE: libc::c_int = 2;

/// A vsock stream socket with a receive window of `RELAY_VSOCK_BUFFER`
/// bytes. Linux's default, 256 KiB, is the window the guest offers the
/// host, so it caps the host-to-guest leg.
fn vsock_socket() -> io::Result<OwnedFd> {
    let fd = unsafe {
        owned(libc::socket(
            libc::AF_VSOCK,
            libc::SOCK_STREAM | libc::SOCK_CLOEXEC,
            0,
        ))?
    };
    if let Some(size) = env::var("RELAY_VSOCK_BUFFER")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
    {
        setsockopt(
            fd.as_raw_fd(),
            libc::AF_VSOCK,
            SO_VM_SOCKETS_BUFFER_MAX_SIZE,
            &size,
        )?;
        setsockopt(
            fd.as_raw_fd(),
            libc::AF_VSOCK,
            SO_VM_SOCKETS_BUFFER_SIZE,
            &size,
        )?;
    }
    Ok(fd)
}

fn sockaddr(addr: SocketAddrV4) -> libc::sockaddr_in {
    let mut out: libc::sockaddr_in = unsafe { mem::zeroed() };
    out.sin_family = libc::AF_INET as libc::sa_family_t;
    out.sin_port = addr.port().to_be();
    out.sin_addr.s_addr = u32::from(*addr.ip()).to_be();
    out
}

fn from_sockaddr(addr: &libc::sockaddr_in) -> SocketAddrV4 {
    SocketAddrV4::new(
        Ipv4Addr::from(u32::from_be(addr.sin_addr.s_addr)),
        u16::from_be(addr.sin_port),
    )
}

const SOCKADDR_LEN: libc::socklen_t = mem::size_of::<libc::sockaddr_in>() as libc::socklen_t;

fn original_dst(fd: i32) -> io::Result<SocketAddr> {
    let mut addr: libc::sockaddr_in = unsafe { mem::zeroed() };
    let mut len = SOCKADDR_LEN;
    let rc = unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_IP,
            SO_ORIGINAL_DST,
            (&raw mut addr).cast(),
            &raw mut len,
        )
    };
    if rc != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(SocketAddr::V4(from_sockaddr(&addr)))
}

fn vsock_connect(port: u32) -> io::Result<Stream> {
    let fd = vsock_socket()?;
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

/// A forward: the host connects in over vsock, names a guest address,
/// and the relay connects there. The guest sees the router's address.
fn inbound(port: u32) -> io::Result<()> {
    // An accepted socket takes the listener's window.
    let listener = vsock_socket()?;
    let mut addr: libc::sockaddr_vm = unsafe { mem::zeroed() };
    addr.svm_family = libc::AF_VSOCK as libc::sa_family_t;
    addr.svm_cid = libc::VMADDR_CID_ANY;
    addr.svm_port = port;
    let len = mem::size_of::<libc::sockaddr_vm>() as libc::socklen_t;
    if unsafe { libc::bind(listener.as_raw_fd(), (&raw const addr).cast(), len) } != 0
        || unsafe { libc::listen(listener.as_raw_fd(), 128) } != 0
    {
        return Err(io::Error::last_os_error());
    }
    loop {
        let fd = unsafe {
            owned(libc::accept4(
                listener.as_raw_fd(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                libc::SOCK_CLOEXEC,
            ))?
        };
        thread::spawn(move || {
            let mut host = Stream::new(fd);
            let mut raw = [0u8; REQUEST_LEN];
            let result = host.read_exact(&mut raw).and_then(|()| {
                let (_, dst) = decode(&raw)?;
                match std::net::TcpStream::connect(dst) {
                    Ok(guest) => {
                        host.write_all(&[0])?;
                        splice_both(host, Stream::from(guest))
                    }
                    Err(err) => {
                        host.write_all(&[1])?;
                        Err(io::Error::other(format!("forward to {dst}: {err}")))
                    }
                }
            });
            if let Err(err) = result {
                eprintln!("relay: {err}");
            }
        });
    }
}

fn tcp_flow(guest: std::net::TcpStream, vsock_port: u32) -> io::Result<()> {
    let dst = original_dst(guest.as_raw_fd())?;
    let mut host = vsock_connect(vsock_port)?;
    host.write_all(&encode(TCP, dst))?;
    let mut status = [0u8; 1];
    host.read_exact(&mut status)?;
    if status[0] != 0 {
        // The egress could not connect: the guest sees a reset, as it
        // would from a host that refused.
        let linger = libc::linger {
            l_onoff: 1,
            l_linger: 0,
        };
        setsockopt(
            guest.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_LINGER,
            &linger,
        )?;
        return Ok(());
    }
    splice_both(Stream::from(guest), host)
}

fn udp_socket() -> io::Result<OwnedFd> {
    let fd = unsafe {
        owned(libc::socket(
            libc::AF_INET,
            libc::SOCK_DGRAM | libc::SOCK_CLOEXEC,
            0,
        ))?
    };
    setsockopt(fd.as_raw_fd(), libc::SOL_IP, libc::IP_TRANSPARENT, &1i32)?;
    Ok(fd)
}

fn bind(fd: &OwnedFd, addr: SocketAddrV4) -> io::Result<()> {
    let sa = sockaddr(addr);
    if unsafe { libc::bind(fd.as_raw_fd(), (&raw const sa).cast(), SOCKADDR_LEN) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// One datagram, its sender, and the destination it was sent to.
fn recv_original(fd: &OwnedFd, buf: &mut [u8]) -> io::Result<(usize, SocketAddrV4, SocketAddrV4)> {
    let mut src: libc::sockaddr_in = unsafe { mem::zeroed() };
    let mut control = [0u64; 8];
    let mut iov = libc::iovec {
        iov_base: buf.as_mut_ptr().cast(),
        iov_len: buf.len(),
    };
    let mut msg: libc::msghdr = unsafe { mem::zeroed() };
    msg.msg_name = (&raw mut src).cast();
    msg.msg_namelen = SOCKADDR_LEN;
    msg.msg_iov = &raw mut iov;
    msg.msg_iovlen = 1;
    msg.msg_control = control.as_mut_ptr().cast();
    msg.msg_controllen = mem::size_of_val(&control) as _;
    let n = unsafe { libc::recvmsg(fd.as_raw_fd(), &raw mut msg, 0) };
    if n < 0 {
        return Err(io::Error::last_os_error());
    }
    let mut cmsg = unsafe { libc::CMSG_FIRSTHDR(&raw const msg) };
    while !cmsg.is_null() {
        let header = unsafe { &*cmsg };
        if header.cmsg_level == libc::SOL_IP && header.cmsg_type == IP_ORIGDSTADDR {
            let dst =
                unsafe { (libc::CMSG_DATA(cmsg) as *const libc::sockaddr_in).read_unaligned() };
            return Ok((n as usize, from_sockaddr(&src), from_sockaddr(&dst)));
        }
        cmsg = unsafe { libc::CMSG_NXTHDR(&raw const msg, cmsg) };
    }
    Err(io::Error::other("a datagram with no original destination"))
}

type Flows<K> = Arc<Mutex<HashSet<K>>>;

fn udp_flow(
    client: SocketAddrV4,
    dst: SocketAddrV4,
    first: &[u8],
    vsock_port: u32,
) -> io::Result<()> {
    let reply = udp_socket()?;
    // Two guests may talk to one destination: each flow binds it.
    setsockopt(
        reply.as_raw_fd(),
        libc::SOL_SOCKET,
        libc::SO_REUSEADDR,
        &1i32,
    )?;
    bind(&reply, dst)?;
    let reply = UdpSocket::from(reply);
    reply.connect(client)?;
    let mut host = vsock_connect(vsock_port)?;
    host.write_all(&encode(UDP, SocketAddr::V4(dst)))?;
    host.write_frame_or_drop(first)?;
    pump(host, reply)
}

fn udp_listener(port: u16, vsock_port: u32) -> io::Result<()> {
    let listener = udp_socket()?;
    setsockopt(
        listener.as_raw_fd(),
        libc::SOL_IP,
        libc::IP_RECVORIGDSTADDR,
        &1i32,
    )?;
    bind(&listener, SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, port))?;
    let flows: Flows<(SocketAddrV4, SocketAddrV4)> = Arc::default();
    let mut buf = vec![0u8; 65536];
    loop {
        let (n, client, dst) = recv_original(&listener, &mut buf)?;
        // A datagram that races its flow's own socket is lost.
        if !flows.lock().unwrap().insert((client, dst)) {
            continue;
        }
        let first = buf[..n].to_vec();
        let flows = flows.clone();
        thread::spawn(move || {
            if let Err(err) = udp_flow(client, dst, &first, vsock_port) {
                eprintln!("relay: udp {client} -> {dst}: {err}");
            }
            flows.lock().unwrap().remove(&(client, dst));
        });
    }
}

type EchoKey = (Ipv4Addr, Ipv4Addr, u16);

struct Echo {
    host: Stream,
    activity: Arc<Activity>,
}

/// Echo replies from the host, given the guest's echo id and sent to it
/// as if from the destination.
fn echo_replies(
    mut host: Stream,
    key: EchoKey,
    activity: Arc<Activity>,
    raw: Arc<OwnedFd>,
) -> io::Result<()> {
    let (client, dst, id) = key;
    host.set_read_timeout(TICK)?;
    let mut buf = vec![0u8; 65536];
    loop {
        let n = match host.read_frame(&mut buf[20..]) {
            Ok(n) => n,
            Err(err) if timed_out(&err) && !activity.idle() => continue,
            Err(_) => return Ok(()),
        };
        activity.touch();
        let packet = &mut buf[..20 + n];
        let icmp = &mut packet[20..];
        icmp[4..6].copy_from_slice(&id.to_be_bytes());
        icmp[2..4].fill(0);
        let sum = checksum(icmp);
        icmp[2..4].copy_from_slice(&sum.to_be_bytes());
        let ip = &mut packet[..20];
        ip.fill(0);
        ip[0] = 0x45;
        ip[2..4].copy_from_slice(&((20 + n) as u16).to_be_bytes());
        ip[8] = 64;
        ip[9] = ICMP;
        ip[12..16].copy_from_slice(&dst.octets());
        ip[16..20].copy_from_slice(&client.octets());
        let sum = checksum(ip);
        ip[10..12].copy_from_slice(&sum.to_be_bytes());
        let to = sockaddr(SocketAddrV4::new(client, 0));
        unsafe {
            libc::sendto(
                raw.as_raw_fd(),
                packet.as_ptr().cast(),
                packet.len(),
                0,
                (&raw const to).cast(),
                SOCKADDR_LEN,
            )
        };
    }
}

fn icmp_echo(vsock_port: u32) -> io::Result<()> {
    let requests = unsafe {
        owned(libc::socket(
            libc::AF_INET,
            libc::SOCK_RAW | libc::SOCK_CLOEXEC,
            libc::IPPROTO_ICMP,
        ))?
    };
    let raw = Arc::new(unsafe {
        owned(libc::socket(
            libc::AF_INET,
            libc::SOCK_RAW | libc::SOCK_CLOEXEC,
            libc::IPPROTO_RAW,
        ))?
    });
    let flows: Arc<Mutex<HashMap<EchoKey, Echo>>> = Arc::default();
    let mut buf = vec![0u8; 65536];
    loop {
        let n = unsafe { libc::recv(requests.as_raw_fd(), buf.as_mut_ptr().cast(), buf.len(), 0) };
        if n < 0 {
            return Err(io::Error::last_os_error());
        }
        let packet = &buf[..n as usize];
        let ihl = usize::from(packet[0] & 0x0f) * 4;
        if packet.len() < ihl + 8 || packet[ihl] != 8 {
            continue;
        }
        let client = Ipv4Addr::new(packet[12], packet[13], packet[14], packet[15]);
        let dst = Ipv4Addr::new(packet[16], packet[17], packet[18], packet[19]);
        let id = u16::from_be_bytes([packet[ihl + 4], packet[ihl + 5]]);
        let key = (client, dst, id);
        let message = &packet[ihl..];
        let mut table = flows.lock().unwrap();
        if let Some(echo) = table.get_mut(&key) {
            echo.activity.touch();
            if echo.host.write_frame_or_drop(message).is_ok() {
                continue;
            }
            table.remove(&key);
        }
        let opened = vsock_connect(vsock_port).and_then(|mut host| {
            host.write_all(&encode(ICMP, SocketAddr::V4(SocketAddrV4::new(dst, 0))))?;
            host.write_frame_or_drop(message)?;
            Ok(host)
        });
        let host = match opened {
            Ok(host) => host,
            Err(err) => {
                eprintln!("relay: icmp {client} -> {dst}: {err}");
                continue;
            }
        };
        let activity = Arc::new(Activity::default());
        activity.touch();
        let back = host.try_clone()?;
        table.insert(
            key,
            Echo {
                host,
                activity: activity.clone(),
            },
        );
        drop(table);
        let (flows, raw) = (flows.clone(), raw.clone());
        thread::spawn(move || {
            let _ = echo_replies(back, key, activity, raw);
            if let Some(echo) = flows.lock().unwrap().remove(&key) {
                echo.host.shutdown(Shutdown::Both);
            }
        });
    }
}

fn main() -> io::Result<()> {
    let args: Vec<String> = env::args().collect();
    let tcp: u16 = args
        .get(1)
        .map_or(Ok(9000), |a| a.parse())
        .map_err(io::Error::other)?;
    let vsock_port: u32 = args
        .get(2)
        .map_or(Ok(5000), |a| a.parse())
        .map_err(io::Error::other)?;
    let udp: u16 = args
        .get(3)
        .map_or(Ok(9001), |a| a.parse())
        .map_err(io::Error::other)?;
    let in_port: u32 = args
        .get(4)
        .map_or(Ok(5001), |a| a.parse())
        .map_err(io::Error::other)?;
    if env::var("RELAY_YIELD").is_ok_and(|value| !value.is_empty() && value != "0") {
        vivarium_egress::YIELD_BEFORE_READ.store(true, std::sync::atomic::Ordering::Relaxed);
    }
    thread::spawn(move || {
        if let Err(err) = inbound(in_port) {
            eprintln!("relay: inbound: {err}");
        }
    });
    thread::spawn(move || {
        if let Err(err) = udp_listener(udp, vsock_port) {
            eprintln!("relay: udp listener: {err}");
        }
    });
    thread::spawn(move || {
        if let Err(err) = icmp_echo(vsock_port) {
            eprintln!("relay: icmp: {err}");
        }
    });
    let listener = TcpListener::bind(("0.0.0.0", tcp))?;
    eprintln!("relay: tcp {tcp}, udp {udp}, icmp echo, to vsock {HOST_CID}:{vsock_port}");
    for guest in listener.incoming() {
        let guest = guest?;
        thread::spawn(move || {
            if let Err(err) = tcp_flow(guest, vsock_port) {
                eprintln!("relay: {err}");
            }
        });
    }
    Ok(())
}

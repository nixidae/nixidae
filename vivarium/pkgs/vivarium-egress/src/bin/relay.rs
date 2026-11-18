//! In the router guest. Each flow a guest sends through the router
//! becomes one vsock stream to the host's egress, which opens the real
//! socket.
//!
//! Each of TCP, UDP and ICMP echo has an IPv4 and an IPv6 socket. The
//! IPv6 ones are `IPV6_V6ONLY`: `SO_ORIGINAL_DST` at `SOL_IP` fails on an
//! IPv6 socket, so a v4-mapped one would lose the destination.
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
//!   the guest's id back before it goes out on a raw socket. ICMPv6 has
//!   no header on a raw socket: `IPV6_PKTINFO` names the destination, and
//!   sets the reply's source.
//!
//! - Forwards: the host connects in on vsock IN_PORT and names a guest
//!   address; the relay connects to it.
//!
//!     relay TCP_PORT VSOCK_PORT UDP_PORT IN_PORT
//!
//! VSOCK_PORT and IN_PORT also take `unix:PATH`, for a guest with no vsock
//! device: a container, whose unix sockets the host shares.

use std::collections::HashMap;
use std::collections::HashSet;
use std::env;
use std::ffi::CString;
use std::fmt;
use std::fs;
use std::io::{self, Read, Write};
use std::mem;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, Shutdown, SocketAddr, TcpListener, UdpSocket};
use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;

use vivarium_egress::{
    Activity, ICMP, REQUEST_LEN, Stream, TCP, TICK, UDP, bind, checksum, connect, decode, encode,
    family, from_sockaddr, owned, pump, setsockopt, sockaddr, splice_both, timed_out,
};

/// `SO_ORIGINAL_DST` at `SOL_IP`, and `IP6T_SO_ORIGINAL_DST` at `SOL_IPV6`.
const SO_ORIGINAL_DST: libc::c_int = 80;
/// `IP_ORIGDSTADDR`, the control message `IP_RECVORIGDSTADDR` asks for.
const IP_ORIGDSTADDR: libc::c_int = 20;
const IPV6_ORIGDSTADDR: libc::c_int = 74;
const IPV6_RECVORIGDSTADDR: libc::c_int = 74;
const IPV6_TRANSPARENT: libc::c_int = 75;
/// `ICMP6_FILTER` at `SOL_ICMPV6`: a bit set blocks that type.
const ICMP6_FILTER: libc::c_int = 1;
const ICMP6_ECHO_REQUEST: u8 = 128;
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

fn original_dst(fd: i32, v6: bool) -> io::Result<SocketAddr> {
    let mut addr: libc::sockaddr_storage = unsafe { mem::zeroed() };
    let mut len = mem::size_of::<libc::sockaddr_storage>() as libc::socklen_t;
    let level = if v6 { libc::SOL_IPV6 } else { libc::SOL_IP };
    let rc = unsafe {
        libc::getsockopt(
            fd,
            level,
            SO_ORIGINAL_DST,
            (&raw mut addr).cast(),
            &raw mut len,
        )
    };
    if rc != 0 {
        return Err(io::Error::last_os_error());
    }
    unsafe { from_sockaddr((&raw const addr).cast()) }
}

/// `RELAY_DEVICE`: the device, a VRF, that every IP socket binds to. A
/// raw socket bound to no device reads every VRF's packets, so two relays
/// would both answer one echo request.
fn device() -> Option<&'static CString> {
    static DEVICE: OnceLock<Option<CString>> = OnceLock::new();
    DEVICE
        .get_or_init(|| {
            env::var("RELAY_DEVICE")
                .ok()
                .filter(|name| !name.is_empty())
                .and_then(|name| CString::new(name).ok())
        })
        .as_ref()
}

/// An IP socket, bound to `RELAY_DEVICE` if it names one.
fn ip_socket(domain: libc::c_int, kind: libc::c_int, proto: libc::c_int) -> io::Result<OwnedFd> {
    let fd = unsafe { owned(libc::socket(domain, kind | libc::SOCK_CLOEXEC, proto))? };
    if let Some(name) = device() {
        let bytes = name.as_bytes_with_nul();
        let rc = unsafe {
            libc::setsockopt(
                fd.as_raw_fd(),
                libc::SOL_SOCKET,
                libc::SO_BINDTODEVICE,
                bytes.as_ptr().cast(),
                bytes.len() as libc::socklen_t,
            )
        };
        if rc != 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(fd)
}

/// A socket of `addr`'s family, IPv6 only if it is IPv6.
fn socket(addr: SocketAddr, kind: libc::c_int, proto: libc::c_int) -> io::Result<OwnedFd> {
    let fd = ip_socket(family(addr), kind, proto)?;
    if addr.is_ipv6() {
        setsockopt(fd.as_raw_fd(), libc::SOL_IPV6, libc::IPV6_V6ONLY, &1i32)?;
    }
    Ok(fd)
}

/// One end of the relay's link to the host: a vsock port on the host's
/// CID, or a unix socket's path.
#[derive(Clone, Copy)]
enum Endpoint {
    Vsock(u32),
    Unix(&'static Path),
}

impl Endpoint {
    /// `unix:PATH`, or a vsock port.
    fn parse(arg: &str) -> io::Result<Self> {
        match arg.strip_prefix("unix:") {
            // The relay's arguments live as long as the relay.
            Some(path) => Ok(Self::Unix(Box::leak(Path::new(path).into()))),
            None => arg.parse().map(Self::Vsock).map_err(io::Error::other),
        }
    }

    fn connect(self) -> io::Result<Stream> {
        match self {
            Self::Vsock(port) => vsock_connect(port),
            Self::Unix(path) => Ok(Stream::new(UnixStream::connect(path)?.into())),
        }
    }

    fn listen(self) -> io::Result<OwnedFd> {
        match self {
            Self::Vsock(port) => vsock_listen(port),
            Self::Unix(path) => {
                // A restarted relay finds its last socket, and bind refuses it.
                match fs::remove_file(path) {
                    Err(err) if err.kind() != io::ErrorKind::NotFound => return Err(err),
                    _ => {}
                }
                Ok(UnixListener::bind(path)?.into())
            }
        }
    }
}

impl fmt::Display for Endpoint {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Vsock(port) => write!(f, "vsock {HOST_CID}:{port}"),
            Self::Unix(path) => write!(f, "unix {}", path.display()),
        }
    }
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

fn vsock_listen(port: u32) -> io::Result<OwnedFd> {
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
    Ok(listener)
}

/// A forward: the host connects in, names a guest address, and the
/// relay connects there, TCP or UDP. The guest sees the router's
/// address. A guest's UDP reply to it matches the connected socket before
/// the `tproxy` rule's listener: nft's tproxy looks up an established
/// socket first.
fn inbound(from: Endpoint) -> io::Result<()> {
    let listener = from.listen()?;
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
                let (proto, dst) = decode(&raw)?;
                if proto == UDP {
                    // No status byte: the first datagram follows at once.
                    let socket = socket(dst, libc::SOCK_DGRAM, 0)?;
                    connect(&socket, dst)?;
                    return pump(host, UdpSocket::from(socket));
                }
                let guest = socket(dst, libc::SOCK_STREAM, 0)?;
                match connect(&guest, dst) {
                    Ok(()) => {
                        host.write_all(&[0])?;
                        splice_both(host, Stream::new(guest))
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

fn tcp_flow(guest: std::net::TcpStream, egress: Endpoint) -> io::Result<()> {
    let dst = original_dst(guest.as_raw_fd(), guest.local_addr()?.is_ipv6())?;
    let mut host = egress.connect()?;
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

/// A transparent datagram socket of `addr`'s family, bound to `addr`.
fn udp_socket(addr: SocketAddr, reuse: bool) -> io::Result<OwnedFd> {
    let fd = socket(addr, libc::SOCK_DGRAM, 0)?;
    let (level, transparent) = match addr {
        SocketAddr::V4(_) => (libc::SOL_IP, libc::IP_TRANSPARENT),
        SocketAddr::V6(_) => (libc::SOL_IPV6, IPV6_TRANSPARENT),
    };
    setsockopt(fd.as_raw_fd(), level, transparent, &1i32)?;
    if reuse {
        setsockopt(fd.as_raw_fd(), libc::SOL_SOCKET, libc::SO_REUSEADDR, &1i32)?;
    }
    bind(&fd, addr)?;
    Ok(fd)
}

/// One datagram, its sender, and the destination it was sent to.
fn recv_original(fd: &OwnedFd, buf: &mut [u8]) -> io::Result<(usize, SocketAddr, SocketAddr)> {
    let mut src: libc::sockaddr_storage = unsafe { mem::zeroed() };
    let mut control = [0u64; 8];
    let mut iov = libc::iovec {
        iov_base: buf.as_mut_ptr().cast(),
        iov_len: buf.len(),
    };
    let mut msg: libc::msghdr = unsafe { mem::zeroed() };
    msg.msg_name = (&raw mut src).cast();
    msg.msg_namelen = mem::size_of_val(&src) as libc::socklen_t;
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
        if (header.cmsg_level, header.cmsg_type) == (libc::SOL_IP, IP_ORIGDSTADDR)
            || (header.cmsg_level, header.cmsg_type) == (libc::SOL_IPV6, IPV6_ORIGDSTADDR)
        {
            let src = unsafe { from_sockaddr((&raw const src).cast())? };
            let dst = unsafe { from_sockaddr(libc::CMSG_DATA(cmsg).cast())? };
            return Ok((n as usize, src, dst));
        }
        cmsg = unsafe { libc::CMSG_NXTHDR(&raw const msg, cmsg) };
    }
    Err(io::Error::other("a datagram with no original destination"))
}

type Flows<K> = Arc<Mutex<HashSet<K>>>;

fn udp_flow(client: SocketAddr, dst: SocketAddr, first: &[u8], egress: Endpoint) -> io::Result<()> {
    // Two guests may talk to one destination: each flow binds it.
    let reply = UdpSocket::from(udp_socket(dst, true)?);
    reply.connect(client)?;
    let mut host = egress.connect()?;
    host.write_all(&encode(UDP, dst))?;
    host.write_frame_or_drop(first)?;
    pump(host, reply)
}

fn udp_listener(addr: SocketAddr, egress: Endpoint) -> io::Result<()> {
    let listener = udp_socket(addr, false)?;
    let (level, option) = match addr {
        SocketAddr::V4(_) => (libc::SOL_IP, libc::IP_RECVORIGDSTADDR),
        SocketAddr::V6(_) => (libc::SOL_IPV6, IPV6_RECVORIGDSTADDR),
    };
    setsockopt(listener.as_raw_fd(), level, option, &1i32)?;
    let flows: Flows<(SocketAddr, SocketAddr)> = Arc::default();
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
            if let Err(err) = udp_flow(client, dst, &first, egress) {
                eprintln!("relay: udp {client} -> {dst}: {err}");
            }
            flows.lock().unwrap().remove(&(client, dst));
        });
    }
}

/// The guest, the destination it pinged, and its echo id.
type EchoKey = (IpAddr, IpAddr, u16);

struct Echo {
    host: Stream,
    activity: Arc<Activity>,
}

type Echoes = Arc<Mutex<HashMap<EchoKey, Echo>>>;

/// Sends `icmp`, an ICMP message, from `from` to `to` on `raw`: an
/// `IPPROTO_RAW` socket for IPv4, with `buf[..20]` free for the header;
/// a transparent ICMPv6 socket for IPv6, which sums the message itself.
fn send_echo(raw: &OwnedFd, buf: &mut [u8], n: usize, from: IpAddr, to: IpAddr) {
    let (packet, ancillary) = match (from, to) {
        (IpAddr::V4(from), IpAddr::V4(to)) => {
            let packet = &mut buf[..20 + n];
            let icmp = &mut packet[20..];
            icmp[2..4].fill(0);
            let sum = checksum(icmp);
            icmp[2..4].copy_from_slice(&sum.to_be_bytes());
            let ip = &mut packet[..20];
            ip.fill(0);
            ip[0] = 0x45;
            ip[2..4].copy_from_slice(&((20 + n) as u16).to_be_bytes());
            ip[8] = 64;
            ip[9] = ICMP;
            ip[12..16].copy_from_slice(&from.octets());
            ip[16..20].copy_from_slice(&to.octets());
            let sum = checksum(ip);
            ip[10..12].copy_from_slice(&sum.to_be_bytes());
            (&buf[..20 + n], None)
        }
        (IpAddr::V6(from), IpAddr::V6(_)) => (
            &buf[20..20 + n],
            Some(libc::in6_pktinfo {
                ipi6_addr: libc::in6_addr {
                    s6_addr: from.octets(),
                },
                ipi6_ifindex: 0,
            }),
        ),
        _ => return,
    };
    let (mut name, name_len) = sockaddr(SocketAddr::new(to, 0));
    let mut iov = libc::iovec {
        iov_base: packet.as_ptr().cast_mut().cast(),
        iov_len: packet.len(),
    };
    let mut control = [0u64; 8];
    let mut msg: libc::msghdr = unsafe { mem::zeroed() };
    msg.msg_name = (&raw mut name).cast();
    msg.msg_namelen = name_len;
    msg.msg_iov = &raw mut iov;
    msg.msg_iovlen = 1;
    if let Some(info) = ancillary {
        let space = unsafe { libc::CMSG_SPACE(mem::size_of::<libc::in6_pktinfo>() as u32) };
        msg.msg_control = control.as_mut_ptr().cast();
        msg.msg_controllen = space as _;
        unsafe {
            let cmsg = libc::CMSG_FIRSTHDR(&raw const msg);
            (*cmsg).cmsg_level = libc::SOL_IPV6;
            (*cmsg).cmsg_type = libc::IPV6_PKTINFO;
            (*cmsg).cmsg_len = libc::CMSG_LEN(mem::size_of::<libc::in6_pktinfo>() as u32) as _;
            libc::CMSG_DATA(cmsg)
                .cast::<libc::in6_pktinfo>()
                .write_unaligned(info);
        }
    }
    unsafe { libc::sendmsg(raw.as_raw_fd(), &raw const msg, 0) };
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
        buf[24..26].copy_from_slice(&id.to_be_bytes());
        send_echo(&raw, &mut buf, n, dst, client);
    }
}

/// One echo request from a guest: onto its flow's stream, or a new flow.
fn echo_request(
    echoes: &Echoes,
    key: EchoKey,
    message: &[u8],
    egress: Endpoint,
    raw: &Arc<OwnedFd>,
) {
    let (client, dst, _) = key;
    let mut table = echoes.lock().unwrap();
    if let Some(echo) = table.get_mut(&key) {
        echo.activity.touch();
        if echo.host.write_frame_or_drop(message).is_ok() {
            return;
        }
        table.remove(&key);
    }
    let opened = egress.connect().and_then(|mut host| {
        host.write_all(&encode(ICMP, SocketAddr::new(dst, 0)))?;
        host.write_frame_or_drop(message)?;
        Ok(host)
    });
    let opened = opened.and_then(|host| Ok((host.try_clone()?, host)));
    let (host, back) = match opened {
        Ok(pair) => pair,
        Err(err) => {
            eprintln!("relay: icmp {client} -> {dst}: {err}");
            return;
        }
    };
    let activity = Arc::new(Activity::default());
    activity.touch();
    table.insert(
        key,
        Echo {
            host,
            activity: activity.clone(),
        },
    );
    drop(table);
    let (echoes, raw) = (echoes.clone(), raw.clone());
    thread::spawn(move || {
        let _ = echo_replies(back, key, activity, raw);
        if let Some(echo) = echoes.lock().unwrap().remove(&key) {
            echo.host.shutdown(Shutdown::Both);
        }
    });
}

fn icmp_echo(egress: Endpoint, echoes: Echoes) -> io::Result<()> {
    let requests = ip_socket(libc::AF_INET, libc::SOCK_RAW, libc::IPPROTO_ICMP)?;
    let raw = Arc::new(ip_socket(libc::AF_INET, libc::SOCK_RAW, libc::IPPROTO_RAW)?);
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
        let key = (IpAddr::V4(client), IpAddr::V4(dst), id);
        echo_request(&echoes, key, &packet[ihl..], egress, &raw);
    }
}

fn icmp6_echo(egress: Endpoint, echoes: Echoes) -> io::Result<()> {
    let fd = ip_socket(libc::AF_INET6, libc::SOCK_RAW, libc::IPPROTO_ICMPV6)?;
    let mut filter = [u32::MAX; 8];
    filter[usize::from(ICMP6_ECHO_REQUEST) >> 5] &= !(1 << (ICMP6_ECHO_REQUEST & 31));
    setsockopt(fd.as_raw_fd(), libc::SOL_ICMPV6, ICMP6_FILTER, &filter)?;
    setsockopt(
        fd.as_raw_fd(),
        libc::SOL_IPV6,
        libc::IPV6_RECVPKTINFO,
        &1i32,
    )?;
    // The reply's source is the address the guest pinged, not the router's.
    setsockopt(fd.as_raw_fd(), libc::SOL_IPV6, IPV6_TRANSPARENT, &1i32)?;
    let raw = Arc::new(fd);
    let mut buf = vec![0u8; 65536];
    loop {
        let mut src: libc::sockaddr_in6 = unsafe { mem::zeroed() };
        let mut control = [0u64; 8];
        let mut iov = libc::iovec {
            iov_base: buf.as_mut_ptr().cast(),
            iov_len: buf.len(),
        };
        let mut msg: libc::msghdr = unsafe { mem::zeroed() };
        msg.msg_name = (&raw mut src).cast();
        msg.msg_namelen = mem::size_of_val(&src) as libc::socklen_t;
        msg.msg_iov = &raw mut iov;
        msg.msg_iovlen = 1;
        msg.msg_control = control.as_mut_ptr().cast();
        msg.msg_controllen = mem::size_of_val(&control) as _;
        let n = unsafe { libc::recvmsg(raw.as_raw_fd(), &raw mut msg, 0) };
        if n < 0 {
            return Err(io::Error::last_os_error());
        }
        let mut dst = None;
        let mut cmsg = unsafe { libc::CMSG_FIRSTHDR(&raw const msg) };
        while !cmsg.is_null() {
            let header = unsafe { &*cmsg };
            if (header.cmsg_level, header.cmsg_type) == (libc::SOL_IPV6, libc::IPV6_PKTINFO) {
                let info = unsafe {
                    libc::CMSG_DATA(cmsg)
                        .cast::<libc::in6_pktinfo>()
                        .read_unaligned()
                };
                dst = Some(Ipv6Addr::from(info.ipi6_addr.s6_addr));
            }
            cmsg = unsafe { libc::CMSG_NXTHDR(&raw const msg, cmsg) };
        }
        let message = &buf[..n as usize];
        let Some(dst) = dst else { continue };
        // Only what the guest sent through the router: the link's own
        // addresses have no host behind them.
        if message.len() < 8
            || message[0] != ICMP6_ECHO_REQUEST
            || dst.is_multicast()
            || dst.is_unicast_link_local()
        {
            continue;
        }
        let client = Ipv6Addr::from(src.sin6_addr.s6_addr);
        let id = u16::from_be_bytes([message[4], message[5]]);
        let key = (IpAddr::V6(client), IpAddr::V6(dst), id);
        echo_request(&echoes, key, message, egress, &raw);
    }
}

/// A TCP listener on `addr`, IPv6 only if it is IPv6.
fn tcp_listener(addr: SocketAddr) -> io::Result<TcpListener> {
    let fd = socket(addr, libc::SOCK_STREAM, 0)?;
    setsockopt(fd.as_raw_fd(), libc::SOL_SOCKET, libc::SO_REUSEADDR, &1i32)?;
    bind(&fd, addr)?;
    if unsafe { libc::listen(fd.as_raw_fd(), 1024) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(TcpListener::from(fd))
}

fn tcp_accept(listener: TcpListener, egress: Endpoint) -> io::Result<()> {
    for guest in listener.incoming() {
        let guest = guest?;
        thread::spawn(move || {
            if let Err(err) = tcp_flow(guest, egress) {
                eprintln!("relay: {err}");
            }
        });
    }
    Ok(())
}

/// Runs `body` on a thread of its own; its error ends the relay, so the
/// unit restarts it whole.
fn spawn(what: &'static str, body: impl FnOnce() -> io::Result<()> + Send + 'static) {
    thread::spawn(move || {
        if let Err(err) = body() {
            eprintln!("relay: {what}: {err}");
            std::process::exit(1);
        }
    });
}

fn main() -> io::Result<()> {
    let args: Vec<String> = env::args().collect();
    let tcp: u16 = args
        .get(1)
        .map_or(Ok(9000), |a| a.parse())
        .map_err(io::Error::other)?;
    let egress = args.get(2).map_or(Ok(Endpoint::Vsock(5000)), |a| Endpoint::parse(a))?;
    let udp: u16 = args
        .get(3)
        .map_or(Ok(9001), |a| a.parse())
        .map_err(io::Error::other)?;
    let inward = args.get(4).map_or(Ok(Endpoint::Vsock(5001)), |a| Endpoint::parse(a))?;
    if env::var("RELAY_YIELD").is_ok_and(|value| !value.is_empty() && value != "0") {
        vivarium_egress::YIELD_BEFORE_READ.store(true, std::sync::atomic::Ordering::Relaxed);
    }
    let any4 = |port| SocketAddr::new(IpAddr::V4(Ipv4Addr::UNSPECIFIED), port);
    let any6 = |port| SocketAddr::new(IpAddr::V6(Ipv6Addr::UNSPECIFIED), port);
    let echoes = Echoes::default();
    let (tcp4, tcp6) = (tcp_listener(any4(tcp))?, tcp_listener(any6(tcp))?);
    spawn("inbound", move || inbound(inward));
    spawn("udp", move || udp_listener(any4(udp), egress));
    spawn("udp6", move || udp_listener(any6(udp), egress));
    let echoes6 = echoes.clone();
    spawn("icmp", move || icmp_echo(egress, echoes));
    spawn("icmp6", move || icmp6_echo(egress, echoes6));
    spawn("tcp6", move || tcp_accept(tcp6, egress));
    eprintln!(
        "relay: tcp {tcp}, udp {udp}, icmp echo, IPv4 and IPv6, to {egress}, forwards from {inward}"
    );
    tcp_accept(tcp4, egress)
}

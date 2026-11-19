//! On the host. Each connection on the listen socket is one flow from
//! the router's relay: a request naming the destination, then the bytes
//! (TCP) or length-prefixed datagrams (UDP, ICMP echo).
//! vhost-device-vsock and libkrun both hand a guest's vsock stream to a
//! unix socket, so this serves either.
//!
//!     egress LISTEN_PATH [GATEWAY [FORWARD...]] [--loopback=ADDR...] [--map=FROM=TO...] [--outbound=ADDR...] [--network=PATH...]
//!
//! A flow to GATEWAY, or to a `--loopback` ADDR, goes to the host's
//! loopback of its family, as passt's `--map-host-loopback`. `--map` sends
//! a flow to the ADDR:PORT FROM to TO instead: the guests' resolver to the
//! upstream one, as `--dns-forward`. `--outbound` binds every outside
//! socket of ADDR's family to ADDR, as passt's: 127.0.0.1 and ::1 reach
//! nothing off this host.
//!
//! A flow on LISTEN_PATH, or on the device's `UPLINK_PORT`, is from the
//! router's uplink. A flow on a `--network` PATH, or on any other port of
//! the device, is from one of the router's networks
//! (nixidae/nixidae#1523): it reaches no host port a FORWARD binds. On
//! macOS every forward binds 127.0.0.1, which GATEWAY reaches.
//!
//! FORWARD is `[tcp/|udp/]HOST=GUEST_ADDR:PORT@ROUTER`. HOST is
//! `ADDR:FIRST[-LAST]`, each host port reaching the guest port at the same
//! offset from PORT, or a bare port on 127.0.0.1. A port that will not bind
//! is an error. ROUTER is the path libkrun listens on for the relay, or
//! `vsock:PORT` on the device. Every listener is bound before the device's
//! socket appears.
//!
//! `--control=FD` is a stream socket whose other end the runner holds.
//! Each message is one line, `add PROTO GUEST_ADDR:PORT ROUTER`, with one
//! socket the runner bound (`SCM_RIGHTS`): a TCP listener or a UDP socket.
//! The runner binds, so the bind is the claim of the port, and no other
//! process takes it between a choice and a bind (nixidae/nixidae#1546).
//! The egress answers each with one line, `ok` or `error WHY`. The end of
//! the stream ends the egress.

use std::collections::HashMap;
use std::env;
use std::io::{self, Read, Write};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, TcpListener, TcpStream, UdpSocket};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::{Arc, Mutex, RwLock};
use std::thread;

#[cfg(all(target_os = "linux", feature = "vhost-user"))]
use vivarium_egress::vsock;

use vivarium_egress::{
    ICMP, REQUEST_LEN, Stream, TCP, UDP, bind, connect, decode, echo_reply, encode, family, owned,
    pump, pump_map, socket, splice_both, widen_unix,
};

/// Where a flow comes from: the router's uplink, or one of its networks.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Network {
    Uplink,
    Data,
}

/// The VSOCK_PORT of the uplink's relay, in modules/router.nix. Every
/// other port of the device is a network's.
#[cfg(all(target_os = "linux", feature = "vhost-user"))]
const UPLINK_PORT: u32 = 5000;

/// The host ports one FORWARD binds.
#[derive(Clone, Copy, Debug)]
struct Ports {
    proto: u8,
    bind: IpAddr,
    first: u16,
    last: u16,
}

impl Ports {
    /// Whether a flow of `proto` to `dst` reaches these listeners. `local`
    /// tells if an address is one of the host's: a listener on the
    /// unspecified address takes each of them, and on `::` IPv4 too.
    fn hold(&self, proto: u8, dst: SocketAddr, local: impl Fn(IpAddr) -> bool) -> bool {
        proto == self.proto
            && (self.first..=self.last).contains(&dst.port())
            && (self.bind == dst.ip()
                || self.bind.is_unspecified()
                    && (self.bind.is_ipv6() || dst.is_ipv4())
                    && local(dst.ip()))
    }
}

/// `dst` as the kernel connects to it: an IPv4-mapped IPv6 address is
/// IPv4, and the unspecified address is the loopback of its family. Both
/// reach a listener on 127.0.0.1 (measured), so `allows` sees this form.
fn canonical(dst: SocketAddr) -> SocketAddr {
    let ip = match dst.ip().to_canonical() {
        IpAddr::V4(ip) if ip.is_unspecified() => IpAddr::V4(Ipv4Addr::LOCALHOST),
        IpAddr::V6(ip) if ip.is_unspecified() => IpAddr::V6(Ipv6Addr::LOCALHOST),
        ip => ip,
    };
    SocketAddr::new(ip, dst.port())
}

/// Whether `ip` is one of the host's addresses: a socket binds to it.
fn local(ip: IpAddr) -> bool {
    ip.is_loopback() || UdpSocket::bind(SocketAddr::new(ip, 0)).is_ok()
}

/// Where a flow from the router goes on the host's side.
#[derive(Default)]
struct Outside {
    loopback: Vec<IpAddr>,
    maps: Vec<(SocketAddr, SocketAddr)>,
    outbound: Vec<IpAddr>,
    /// Grows while the egress runs, as the runner sends listeners.
    forwards: RwLock<Vec<Ports>>,
}

impl Outside {
    /// Whether a flow from `network` may go to `dst`, the destination
    /// after `destination`. A network's flow reaches no forward: the
    /// forwards are the uplink's guests'.
    fn allows(&self, network: Network, proto: u8, dst: SocketAddr) -> bool {
        network == Network::Uplink
            || !self
                .forwards
                .read()
                .unwrap()
                .iter()
                .any(|ports| ports.hold(proto, dst, local))
    }

    fn destination(&self, dst: SocketAddr) -> SocketAddr {
        let mut dst = canonical(dst);
        if let Some((_, to)) = self.maps.iter().find(|(from, _)| *from == dst) {
            return *to;
        }
        if self.loopback.contains(&dst.ip()) {
            dst.set_ip(match dst {
                SocketAddr::V4(_) => IpAddr::V4(Ipv4Addr::LOCALHOST),
                SocketAddr::V6(_) => IpAddr::V6(Ipv6Addr::LOCALHOST),
            });
        }
        dst
    }

    /// A socket of `kind` and `proto` connected to `dst`, bound first to
    /// the `--outbound` address of `dst`'s family if there is one.
    fn connect(&self, dst: SocketAddr, kind: i32, proto: i32) -> io::Result<OwnedFd> {
        let fd = socket(family(dst), kind, proto)?;
        if let Some(ip) = self
            .outbound
            .iter()
            .find(|ip| ip.is_ipv4() == dst.is_ipv4())
        {
            bind(&fd, SocketAddr::new(*ip, 0))?;
        }
        connect(&fd, dst)?;
        Ok(fd)
    }
}

fn flow(mut relay: UnixStream, outside: &Outside, network: Network) -> io::Result<()> {
    widen_unix(&relay)?;
    let mut raw = [0u8; REQUEST_LEN];
    relay.read_exact(&mut raw)?;
    let (proto, dst) = decode(&raw)?;
    let dst = outside.destination(dst);
    if !outside.allows(network, proto, dst) {
        // A UDP flow has no status byte: the relay sees the end.
        if proto == TCP {
            relay.write_all(&[1])?;
        }
        return Err(io::Error::other(format!(
            "{dst}: a forward's port, and the flow is from a router network"
        )));
    }
    match proto {
        TCP => {}
        UDP => {
            let fd = outside.connect(dst, libc::SOCK_DGRAM, 0)?;
            return pump(Stream::from(relay), UdpSocket::from(fd));
        }
        ICMP => {
            // A ping socket: no privilege needed where ping_group_range
            // allows it, and on macOS. The kernel picks the echo id.
            let proto = if dst.is_ipv4() {
                libc::IPPROTO_ICMP
            } else {
                libc::IPPROTO_ICMPV6
            };
            let fd = outside.connect(dst, libc::SOCK_DGRAM, proto)?;
            return pump_map(Stream::from(relay), UdpSocket::from(fd), echo_reply);
        }
        _ => {
            relay.write_all(&[1])?;
            return Err(io::Error::other(format!("protocol {proto} is not served")));
        }
    }
    let connected = outside
        .connect(dst, libc::SOCK_STREAM, 0)
        .map(TcpStream::from);
    let outside = match connected {
        Ok(stream) => stream,
        Err(err) => {
            relay.write_all(&[1])?;
            return Err(io::Error::other(format!("{dst}: {err}")));
        }
    };
    relay.write_all(&[0])?;
    splice_both(Stream::from(relay), Stream::from(outside))
}

/// How the egress reaches the router's relay for a forward.
#[derive(Clone)]
enum Router {
    /// libkrun listens on this path for connections into the guest.
    Path(String),
    #[cfg(all(target_os = "linux", feature = "vhost-user"))]
    Vsock(Arc<vsock::Device>, u32),
}

impl Router {
    fn connect(&self) -> io::Result<UnixStream> {
        match self {
            Router::Path(path) => UnixStream::connect(path),
            #[cfg(all(target_os = "linux", feature = "vhost-user"))]
            Router::Vsock(device, port) => device.connect(*port),
        }
    }
}

fn forward(client: TcpStream, guest: SocketAddr, router: &Router) -> io::Result<()> {
    let mut relay = router.connect()?;
    widen_unix(&relay)?;
    relay.write_all(&encode(TCP, guest))?;
    let mut status = [0u8; 1];
    relay.read_exact(&mut status)?;
    if status[0] != 0 {
        return Err(io::Error::other("the relay could not connect"));
    }
    splice_both(Stream::from(client), Stream::from(relay))
}

#[cfg(all(target_os = "linux", feature = "vhost-user"))]
fn device(outside: &Arc<Outside>) -> io::Result<Arc<vsock::Device>> {
    // Every host port serves egress flows; the port names the network.
    let service = |network| -> vsock::Service {
        let outside = outside.clone();
        Arc::new(move |relay| {
            if let Err(err) = flow(relay, &outside, network) {
                eprintln!("egress: {err}");
            }
        })
    };
    vsock::Device::new(
        GUEST_CID,
        HashMap::from([(UPLINK_PORT, service(Network::Uplink))]),
        Some(service(Network::Data)),
    )
}

/// Each flow on `listener`, from `network`, in a thread of its own.
fn accept(listener: UnixListener, outside: Arc<Outside>, network: Network) -> io::Result<()> {
    for relay in listener.incoming() {
        let relay = relay?;
        let outside = outside.clone();
        thread::spawn(move || {
            if let Err(err) = flow(relay, &outside, network) {
                eprintln!("egress: {err}");
            }
        });
    }
    Ok(())
}

#[cfg(all(target_os = "linux", feature = "vhost-user"))]
const GUEST_CID: u64 = 3;

/// One FORWARD argument.
struct Spec {
    ports: Ports,
    guest: SocketAddr,
    /// ROUTER as written: `router` resolves it once the device exists.
    router: String,
}

fn parse_port(text: &str) -> io::Result<u16> {
    text.parse()
        .map_err(|err| io::Error::other(format!("port {text:?}: {err}")))
}

#[cfg(all(target_os = "linux", feature = "vhost-user"))]
type DeviceRef<'a> = Option<&'a Arc<vsock::Device>>;
#[cfg(not(all(target_os = "linux", feature = "vhost-user")))]
type DeviceRef<'a> = Option<&'a ()>;
#[cfg(all(target_os = "linux", feature = "vhost-user"))]
type Device = Option<Arc<vsock::Device>>;
#[cfg(not(all(target_os = "linux", feature = "vhost-user")))]
type Device = Option<()>;

/// The control socket's token in the `Poller`.
const CONTROL: usize = usize::MAX;

/// One control message: a socket the runner bound, and the guest port it reaches.
struct Add {
    proto: u8,
    guest: SocketAddr,
    router: String,
}

fn parse_add(line: &str) -> io::Result<Add> {
    let bad = |why: &str| io::Error::other(format!("control {line:?}: {why}"));
    let words: Vec<&str> = line.split(' ').collect();
    let ["add", proto, guest, router] = words[..] else {
        return Err(bad("not `add PROTO GUEST_ADDR:PORT ROUTER`"));
    };
    Ok(Add {
        proto: match proto {
            "tcp" => TCP,
            "udp" => UDP,
            _ => return Err(bad("not tcp or udp")),
        },
        guest: guest.parse().map_err(|_| bad("not a guest ADDR:PORT"))?,
        router: router.to_owned(),
    })
}

/// One line from the control socket, and the socket that came with it.
/// `None` at the end of the stream: the runner is gone. The runner sends
/// the next line only after the answer, so a read never takes two.
fn recv_line(control: &UnixStream) -> io::Result<Option<(String, Option<OwnedFd>)>> {
    let mut line = Vec::new();
    let mut fd = None;
    loop {
        let mut buf = [0u8; 512];
        // u64, for the alignment a cmsghdr needs.
        let mut space = [0u64; 8];
        let mut iov = libc::iovec {
            iov_base: buf.as_mut_ptr().cast(),
            iov_len: buf.len(),
        };
        let mut msg: libc::msghdr = unsafe { std::mem::zeroed() };
        msg.msg_iov = &raw mut iov;
        msg.msg_iovlen = 1;
        msg.msg_control = space.as_mut_ptr().cast();
        msg.msg_controllen = std::mem::size_of_val(&space) as _;
        let n = unsafe { libc::recvmsg(control.as_raw_fd(), &raw mut msg, 0) };
        if n < 0 {
            let err = io::Error::last_os_error();
            if err.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(err);
        }
        let mut header = unsafe { libc::CMSG_FIRSTHDR(&raw const msg) };
        while !header.is_null() {
            let cmsg = unsafe { &*header };
            if cmsg.cmsg_level == libc::SOL_SOCKET && cmsg.cmsg_type == libc::SCM_RIGHTS {
                let bytes = cmsg.cmsg_len as usize - unsafe { libc::CMSG_LEN(0) } as usize;
                let data = unsafe { libc::CMSG_DATA(header) }.cast::<libc::c_int>();
                for i in 0..bytes / std::mem::size_of::<libc::c_int>() {
                    let raw = unsafe { data.add(i).read_unaligned() };
                    // Owned at once, so a second one closes when dropped.
                    let owned = unsafe { OwnedFd::from_raw_fd(raw) };
                    if unsafe { libc::fcntl(raw, libc::F_SETFD, libc::FD_CLOEXEC) } != 0 {
                        return Err(io::Error::last_os_error());
                    }
                    fd.get_or_insert(owned);
                }
            }
            header = unsafe { libc::CMSG_NXTHDR(&raw const msg, header) };
        }
        if n == 0 {
            return Ok(None);
        }
        line.extend_from_slice(&buf[..n as usize]);
        if line.last() == Some(&b'\n') {
            line.pop();
            let line = String::from_utf8(line).map_err(io::Error::other)?;
            return Ok(Some((line, fd)));
        }
    }
}

/// The socket `add` brings, as a listener the egress serves.
fn adopt(add: &Add, fd: OwnedFd, router: usize) -> io::Result<(Listener, Ports)> {
    let mut kind: libc::c_int = 0;
    let mut len = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
    let rc = unsafe {
        libc::getsockopt(
            fd.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_TYPE,
            (&raw mut kind).cast(),
            &raw mut len,
        )
    };
    if rc != 0 {
        return Err(io::Error::last_os_error());
    }
    let wanted = if add.proto == UDP {
        libc::SOCK_DGRAM
    } else {
        libc::SOCK_STREAM
    };
    if kind != wanted {
        return Err(io::Error::other("the socket is not of the message's protocol"));
    }
    let (socket, local) = if add.proto == UDP {
        let socket = UdpSocket::from(fd);
        socket.set_nonblocking(true)?;
        let local = socket.local_addr()?;
        (Socket::Udp(Arc::new(socket)), local)
    } else {
        let socket = TcpListener::from(fd);
        socket.set_nonblocking(true)?;
        let local = socket.local_addr()?;
        (Socket::Tcp(socket), local)
    };
    let ports = Ports {
        proto: add.proto,
        bind: local.ip(),
        first: local.port(),
        last: local.port(),
    };
    Ok((
        Listener {
            socket,
            guest: add.guest,
            router,
        },
        ports,
    ))
}

fn parse_spec(text: &str) -> io::Result<Spec> {
    let bad = |why: &str| io::Error::other(format!("forward {text:?}: {why}"));
    let (proto, rest) = if let Some(rest) = text.strip_prefix("tcp/") {
        (TCP, rest)
    } else if let Some(rest) = text.strip_prefix("udp/") {
        (UDP, rest)
    } else {
        (TCP, text)
    };
    let (host, rest) = rest.split_once('=').ok_or_else(|| bad("no ="))?;
    let (guest, router) = rest.split_once('@').ok_or_else(|| bad("no @"))?;
    let guest: SocketAddr = guest.parse().map_err(|_| bad("not a guest ADDR:PORT"))?;
    let (bind, ports) = match host.rsplit_once(':') {
        Some((addr, ports)) => (addr.parse().map_err(|_| bad("not a host address"))?, ports),
        None => (IpAddr::V4(Ipv4Addr::LOCALHOST), host),
    };
    let (first, last) = match ports.split_once('-') {
        Some((first, last)) => (parse_port(first)?, parse_port(last)?),
        None => (parse_port(ports)?, parse_port(ports)?),
    };
    if first > last || u32::from(guest.port()) + u32::from(last - first) > 65535 {
        return Err(bad("the guest range leaves 1-65535"));
    }
    Ok(Spec {
        ports: Ports {
            proto,
            bind,
            first,
            last,
        },
        guest,
        router: router.to_owned(),
    })
}

/// A FORWARD's ROUTER: a path, or `vsock:PORT` on the device.
fn router(text: &str, device: DeviceRef) -> io::Result<Router> {
    let bad = |why: &str| io::Error::other(format!("router {text:?}: {why}"));
    match text.strip_prefix("vsock:") {
        #[cfg(all(target_os = "linux", feature = "vhost-user"))]
        Some(port) => Ok(Router::Vsock(
            device
                .ok_or_else(|| bad("vsock: needs vhost-user:"))?
                .clone(),
            port.parse().map_err(|_| bad("not a vsock port"))?,
        )),
        #[cfg(not(all(target_os = "linux", feature = "vhost-user")))]
        Some(_) => {
            let _ = device;
            Err(bad("built without vhost-user"))
        }
        None => Ok(Router::Path(text.to_owned())),
    }
}

enum Socket {
    Tcp(TcpListener),
    Udp(Arc<UdpSocket>),
}

/// A bound host port and the guest port it reaches.
struct Listener {
    socket: Socket,
    guest: SocketAddr,
    router: usize,
}

impl Listener {
    fn fd(&self) -> RawFd {
        match &self.socket {
            Socket::Tcp(socket) => socket.as_raw_fd(),
            Socket::Udp(socket) => socket.as_raw_fd(),
        }
    }
}

/// Every port of every spec, bound and non-blocking.
fn bind_all(specs: &[Spec]) -> io::Result<Vec<Listener>> {
    let mut listeners = Vec::new();
    for (index, spec) in specs.iter().enumerate() {
        let Ports {
            proto,
            bind,
            first,
            last,
        } = spec.ports;
        for port in first..=last {
            let at = SocketAddr::new(bind, port);
            // std sets SO_REUSEADDR on a TCP listener, as passt does.
            let socket = match proto {
                UDP => UdpSocket::bind(at).map(|s| Socket::Udp(Arc::new(s))),
                _ => TcpListener::bind(at).map(Socket::Tcp),
            };
            let socket = match socket {
                Ok(socket) => socket,
                Err(err) if err.raw_os_error() == Some(libc::EMFILE) => {
                    return Err(io::Error::other(format!(
                        "{at}: out of file descriptors after {} listeners",
                        listeners.len()
                    )));
                }
                Err(err) => return Err(io::Error::other(format!("{at}: {err}"))),
            };
            match &socket {
                Socket::Tcp(s) => s.set_nonblocking(true)?,
                Socket::Udp(s) => s.set_nonblocking(true)?,
            }
            let mut guest = spec.guest;
            guest.set_port(spec.guest.port() + (port - first));
            listeners.push(Listener {
                socket,
                guest,
                router: index,
            });
        }
    }
    Ok(listeners)
}

/// The soft limit on open files, raised to the hard one: the router's
/// egress holds every guest's listeners and flows in one process.
fn raise_nofile() -> io::Result<()> {
    let mut limit = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    if unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) } != 0 {
        return Err(io::Error::last_os_error());
    }
    limit.rlim_cur = limit.rlim_max;
    if unsafe { libc::setrlimit(libc::RLIMIT_NOFILE, &limit) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// Readiness of many listeners: epoll on Linux, kqueue on macOS. Not
/// poll(2): macOS refuses it more fds than OPEN_MAX.
struct Poller {
    #[cfg(target_os = "linux")]
    epoll: std::os::fd::OwnedFd,
    #[cfg(not(target_os = "linux"))]
    kqueue: std::os::fd::OwnedFd,
}

impl Poller {
    fn new() -> io::Result<Self> {
        #[cfg(target_os = "linux")]
        let poller = Self {
            epoll: unsafe { owned(libc::epoll_create1(libc::EPOLL_CLOEXEC))? },
        };
        #[cfg(not(target_os = "linux"))]
        let poller = Self {
            kqueue: unsafe { owned(libc::kqueue())? },
        };
        Ok(poller)
    }

    /// Wait on `fd` too; `wait` names it by `token`.
    fn add(&mut self, fd: RawFd, token: usize) -> io::Result<()> {
        #[cfg(target_os = "linux")]
        let rc = {
            let mut event = libc::epoll_event {
                events: libc::EPOLLIN as u32,
                u64: token as u64,
            };
            unsafe { libc::epoll_ctl(self.epoll.as_raw_fd(), libc::EPOLL_CTL_ADD, fd, &mut event) }
        };
        #[cfg(not(target_os = "linux"))]
        let rc = {
            let change = libc::kevent {
                ident: fd as libc::uintptr_t,
                filter: libc::EVFILT_READ,
                flags: libc::EV_ADD,
                fflags: 0,
                data: 0,
                udata: token as *mut libc::c_void,
            };
            unsafe {
                libc::kevent(
                    self.kqueue.as_raw_fd(),
                    &raw const change,
                    1,
                    std::ptr::null_mut(),
                    0,
                    std::ptr::null(),
                )
            }
        };
        if rc != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    /// The indexes of the listeners that are readable.
    fn wait(&mut self, ready: &mut Vec<usize>) -> io::Result<()> {
        ready.clear();
        #[cfg(target_os = "linux")]
        {
            let mut events = [libc::epoll_event { events: 0, u64: 0 }; 256];
            let n =
                unsafe { libc::epoll_wait(self.epoll.as_raw_fd(), events.as_mut_ptr(), 256, -1) };
            if n < 0 {
                let err = io::Error::last_os_error();
                return if err.kind() == io::ErrorKind::Interrupted {
                    Ok(())
                } else {
                    Err(err)
                };
            }
            ready.extend(events[..n as usize].iter().map(|e| e.u64 as usize));
        }
        #[cfg(not(target_os = "linux"))]
        {
            let mut events: [libc::kevent; 256] = unsafe { std::mem::zeroed() };
            let n = unsafe {
                libc::kevent(
                    self.kqueue.as_raw_fd(),
                    std::ptr::null(),
                    0,
                    events.as_mut_ptr(),
                    256,
                    std::ptr::null(),
                )
            };
            if n < 0 {
                let err = io::Error::last_os_error();
                return if err.kind() == io::ErrorKind::Interrupted {
                    Ok(())
                } else {
                    Err(err)
                };
            }
            ready.extend(events[..n as usize].iter().map(|e| e.udata as usize));
        }
        Ok(())
    }
}

/// A UDP flow: one host client on one listener.
type FlowKey = (usize, SocketAddr);
type Flows = Arc<Mutex<HashMap<FlowKey, (u64, Stream)>>>;

/// Datagrams from the guest, back to the client, until the relay ends
/// the flow (idle, as the relay's `pump` decides).
fn udp_back(
    mut relay: Stream,
    socket: Arc<UdpSocket>,
    client: SocketAddr,
    key: FlowKey,
    id: u64,
    flows: Flows,
) {
    let mut buf = vec![0u8; 65536];
    while let Ok(n) = relay.read_frame(&mut buf) {
        // A full socket drops the datagram, as a congested link would.
        let _ = socket.send_to(&buf[..n], client);
    }
    let mut flows = flows.lock().unwrap();
    if flows.get(&key).is_some_and(|(current, _)| *current == id) {
        flows.remove(&key);
    }
}

fn open_udp(router: &Router, guest: SocketAddr) -> io::Result<Stream> {
    let mut relay = router.connect()?;
    relay.write_all(&encode(UDP, guest))?;
    Ok(Stream::from(relay))
}

/// Take the listener a control message brings: answered on the control
/// socket, so a refused one costs the runner its answer and nothing else.
fn take(
    line: &str,
    fd: Option<OwnedFd>,
    listeners: &mut Vec<Listener>,
    routers: &mut Vec<Router>,
    poller: &mut Poller,
    outside: &Outside,
    device: DeviceRef,
) -> io::Result<()> {
    let add = parse_add(line)?;
    let fd = fd.ok_or_else(|| io::Error::other("no socket came with the message"))?;
    let relay = router(&add.router, device)?;
    let (listener, ports) = adopt(&add, fd, routers.len())?;
    poller.add(listener.fd(), listeners.len())?;
    routers.push(relay);
    outside.forwards.write().unwrap().push(ports);
    listeners.push(listener);
    Ok(())
}

fn serve(
    mut listeners: Vec<Listener>,
    mut routers: Vec<Router>,
    control: Option<UnixStream>,
    outside: Arc<Outside>,
    device: Device,
) -> io::Result<()> {
    let mut poller = Poller::new()?;
    for (index, listener) in listeners.iter().enumerate() {
        poller.add(listener.fd(), index)?;
    }
    if let Some(control) = &control {
        poller.add(control.as_raw_fd(), CONTROL)?;
    }
    let flows: Flows = Arc::default();
    let mut next_id = 0u64;
    let mut ready = Vec::new();
    let mut buf = vec![0u8; 65536];
    loop {
        poller.wait(&mut ready)?;
        for &index in &ready {
            if let (CONTROL, Some(control)) = (index, &control) {
                let Some((line, fd)) = recv_line(control)? else {
                    // The runner is gone, and with it every guest this serves.
                    std::process::exit(0);
                };
                let taken = take(
                    &line,
                    fd,
                    &mut listeners,
                    &mut routers,
                    &mut poller,
                    &outside,
                    device.as_ref(),
                );
                let answer = match taken {
                    Ok(()) => "ok\n".to_owned(),
                    Err(err) => format!("error {err}\n"),
                };
                (&*control).write_all(answer.as_bytes())?;
                continue;
            }
            let listener = &listeners[index];
            let router = &routers[listener.router];
            let guest = listener.guest;
            match &listener.socket {
                Socket::Tcp(socket) => loop {
                    let client = match socket.accept() {
                        Ok((client, _)) => client,
                        Err(err) if err.kind() == io::ErrorKind::WouldBlock => break,
                        Err(err) => {
                            eprintln!("egress: accept for {guest}: {err}");
                            break;
                        }
                    };
                    // macOS hands the listener's O_NONBLOCK to the client.
                    if let Err(err) = client.set_nonblocking(false) {
                        eprintln!("egress: forward {guest}: {err}");
                        continue;
                    }
                    let router = router.clone();
                    thread::spawn(move || {
                        if let Err(err) = forward(client, guest, &router) {
                            eprintln!("egress: forward {guest}: {err}");
                        }
                    });
                },
                Socket::Udp(socket) => loop {
                    let (n, client) = match socket.recv_from(&mut buf) {
                        Ok(got) => got,
                        Err(err) if err.kind() == io::ErrorKind::WouldBlock => break,
                        // An ICMP error from an earlier send: UDP goes on.
                        Err(_) => continue,
                    };
                    let key = (index, client);
                    let mut table = flows.lock().unwrap();
                    if !table.contains_key(&key) {
                        let relay = match open_udp(router, guest) {
                            Ok(relay) => relay,
                            Err(err) => {
                                eprintln!("egress: forward udp {guest}: {err}");
                                continue;
                            }
                        };
                        let back = match relay.try_clone() {
                            Ok(back) => back,
                            Err(err) => {
                                eprintln!("egress: forward udp {guest}: {err}");
                                continue;
                            }
                        };
                        next_id += 1;
                        let (socket, flows) = (socket.clone(), flows.clone());
                        let id = next_id;
                        thread::spawn(move || udp_back(back, socket, client, key, id, flows));
                        table.insert(key, (id, relay));
                    }
                    let (_, relay) = table.get_mut(&key).expect("inserted above");
                    if relay.write_frame_or_drop(&buf[..n]).is_err() {
                        table.remove(&key);
                    }
                },
            }
        }
    }
}

fn main() -> io::Result<()> {
    let mut outside = Outside::default();
    let mut args: Vec<String> = Vec::new();
    let mut networks: Vec<String> = Vec::new();
    let mut control = None;
    for arg in env::args() {
        if let Some(fd) = arg.strip_prefix("--control=") {
            let fd: RawFd = fd
                .parse()
                .map_err(|err| io::Error::other(format!("{arg}: {err}")))?;
            if unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) } != 0 {
                return Err(io::Error::other(format!(
                    "{arg}: {}",
                    io::Error::last_os_error()
                )));
            }
            control = Some(unsafe { UnixStream::from_raw_fd(fd) });
        } else if let Some(path) = arg.strip_prefix("--network=") {
            networks.push(path.to_owned());
        } else if let Some(map) = arg.strip_prefix("--map=") {
            let (from, to) = map
                .split_once('=')
                .ok_or_else(|| io::Error::other(format!("{arg}: not FROM=TO")))?;
            let parse = |a: &str| {
                a.parse::<SocketAddr>()
                    .map_err(|err| io::Error::other(format!("{arg}: {err}")))
            };
            outside.maps.push((parse(from)?, parse(to)?));
        } else if let Some(addr) = arg.strip_prefix("--outbound=") {
            outside
                .outbound
                .push(addr.parse().map_err(io::Error::other)?);
        } else if let Some(addr) = arg.strip_prefix("--loopback=") {
            outside
                .loopback
                .push(addr.parse().map_err(io::Error::other)?);
        } else {
            args.push(arg);
        }
    }
    let path = args.get(1).ok_or_else(|| {
        io::Error::other("usage: egress LISTEN_PATH|vhost-user:SOCKET [GATEWAY [FORWARD...]] [--loopback=ADDR] [--map=FROM=TO] [--outbound=ADDR] [--network=PATH] [--control=FD]")
    })?;
    if let Some(gateway) = args.get(2) {
        outside
            .loopback
            .push(gateway.parse().map_err(io::Error::other)?);
    }
    let specs = args[3.min(args.len())..]
        .iter()
        .map(|spec| parse_spec(spec))
        .collect::<io::Result<Vec<_>>>()?;
    outside.forwards = RwLock::new(specs.iter().map(|spec| spec.ports).collect());
    let outside = Arc::new(outside);
    // vhost-user:SOCKET serves the router's vsock device on SOCKET.
    #[cfg(all(target_os = "linux", feature = "vhost-user"))]
    let device = match path.strip_prefix("vhost-user:") {
        Some(socket) => Some((device(&outside)?, socket.to_owned())),
        None => None,
    };
    #[cfg(all(target_os = "linux", feature = "vhost-user"))]
    let device_ref = device.as_ref().map(|(device, _)| device);
    #[cfg(not(all(target_os = "linux", feature = "vhost-user")))]
    let device_ref = None;
    if !specs.is_empty() || control.is_some() {
        raise_nofile()?;
        let listeners = bind_all(&specs)?;
        let routers = specs
            .iter()
            .map(|spec| router(&spec.router, device_ref))
            .collect::<io::Result<Vec<_>>>()?;
        let (outside, device) = (outside.clone(), device_ref.cloned());
        thread::spawn(move || {
            if let Err(err) = serve(listeners, routers, control, outside, device) {
                eprintln!("egress: forwards: {err}");
                std::process::exit(1);
            }
        });
    }
    // Before LISTEN_PATH, which the runner waits for.
    for path in networks {
        let listener = UnixListener::bind(&path)
            .map_err(|err| io::Error::other(format!("--network={path}: {err}")))?;
        let outside = outside.clone();
        thread::spawn(move || {
            if let Err(err) = accept(listener, outside, Network::Data) {
                eprintln!("egress: --network={path}: {err}");
                std::process::exit(1);
            }
        });
    }
    #[cfg(all(target_os = "linux", feature = "vhost-user"))]
    if let Some((device, socket)) = device {
        return device.run(std::path::Path::new(&socket));
    }
    accept(UnixListener::bind(path)?, outside, Network::Uplink)
}

#[cfg(test)]
mod tests {
    use super::{Network, Outside, Ports, Router, parse_add, parse_spec, router};
    use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
    use std::sync::RwLock;
    use vivarium_egress::{ICMP, TCP, UDP};

    fn at(text: &str) -> SocketAddr {
        text.parse().unwrap()
    }

    // The shape `forward.egress_args` writes (test_the_egress_gets_one_port_each
    // in vivarium-runner's tests/test_forward.py), with a libkrun router's
    // socket path: a vsock router needs a device.
    #[test]
    fn a_forward_the_runner_writes_parses() {
        let spec = parse_spec("udp/127.0.0.3:14325=10.0.2.16:22@/run/relay.sock").unwrap();
        assert_eq!(spec.ports.proto, UDP);
        assert_eq!(spec.ports.bind, IpAddr::V4(Ipv4Addr::new(127, 0, 0, 3)));
        assert_eq!((spec.ports.first, spec.ports.last), (14325, 14325));
        assert_eq!(spec.guest, at("10.0.2.16:22"));
        match router(&spec.router, None).unwrap() {
            Router::Path(path) => assert_eq!(path, "/run/relay.sock"),
            #[allow(unreachable_patterns)]
            _ => panic!("a path router"),
        }
    }

    // The line `start_egress` in vivarium-runner's backends/host.py sends.
    #[test]
    fn a_control_add_parses() {
        let add = parse_add("add udp 10.0.2.16:53 /run/relay.sock").unwrap();
        assert_eq!(add.proto, UDP);
        assert_eq!(add.guest, at("10.0.2.16:53"));
        assert_eq!(add.router, "/run/relay.sock");
        for bad in [
            "add sctp 10.0.2.16:53 /r",
            "add tcp 10.0.2.16 /r",
            "add tcp 10.0.2.16:53",
            "remove tcp 10.0.2.16:53 /r",
        ] {
            assert!(parse_add(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn a_range_with_no_protocol_or_address_is_tcp_on_localhost() {
        let spec = parse_spec("8080-8082=10.0.2.15:80@/r").unwrap();
        assert_eq!(spec.ports.proto, TCP);
        assert_eq!(spec.ports.bind, IpAddr::V4(Ipv4Addr::LOCALHOST));
        assert_eq!((spec.ports.first, spec.ports.last), (8080, 8082));
    }

    #[test]
    fn a_range_past_the_last_guest_port_is_refused() {
        assert!(parse_spec("1000-1010=10.0.2.15:65530@/r").is_err());
    }

    fn outside(forwards: &[&str]) -> Outside {
        Outside {
            forwards: RwLock::new(
                forwards
                    .iter()
                    .map(|text| parse_spec(text).unwrap().ports)
                    .collect(),
            ),
            ..Outside::default()
        }
    }

    #[test]
    fn a_network_reaches_no_port_of_a_forward() {
        let outside = outside(&[
            "8080-8082=10.0.2.15:80@/r",
            "udp/127.0.0.3:53=10.0.2.16:53@/r",
        ]);
        for (proto, dst) in [
            (TCP, "127.0.0.1:8080"),
            (TCP, "127.0.0.1:8082"),
            (UDP, "127.0.0.3:53"),
        ] {
            assert!(!outside.allows(Network::Data, proto, at(dst)), "{dst}");
            assert!(outside.allows(Network::Uplink, proto, at(dst)), "{dst}");
        }
    }

    #[test]
    fn a_network_reaches_the_ports_around_a_forward() {
        let outside = outside(&[
            "8080-8082=10.0.2.15:80@/r",
            "udp/127.0.0.3:53=10.0.2.16:53@/r",
        ]);
        for (proto, dst) in [
            (TCP, "127.0.0.1:8079"),
            (TCP, "127.0.0.1:8083"),
            // Another address of the loopback, as each Linux guest has.
            (TCP, "127.0.0.2:8080"),
            (UDP, "127.0.0.1:8080"),
            (TCP, "127.0.0.3:53"),
            (ICMP, "127.0.0.1:0"),
            (TCP, "192.0.2.1:8080"),
        ] {
            assert!(outside.allows(Network::Data, proto, at(dst)), "{dst}");
        }
    }

    #[test]
    fn a_forward_on_the_unspecified_address_holds_the_loopback() {
        let v4 = outside(&["0.0.0.0:8080=10.0.2.15:80@/r"]);
        assert!(!v4.allows(Network::Data, TCP, at("127.0.0.1:8080")));
        assert!(v4.allows(Network::Data, TCP, at("[::1]:8080")));
        // 192.0.2.0/24 is TEST-NET-1: no host holds it.
        assert!(v4.allows(Network::Data, TCP, at("192.0.2.1:8080")));
        // FORWARD has no IPv6 host address.
        let first = v4.forwards.read().unwrap()[0];
        let v6 = Outside {
            forwards: RwLock::new(vec![Ports {
                bind: IpAddr::V6(Ipv6Addr::UNSPECIFIED),
                ..first
            }]),
            ..Outside::default()
        };
        assert!(!v6.allows(Network::Data, TCP, at("[::1]:8080")));
        assert!(!v6.allows(Network::Data, TCP, at("127.0.0.1:8080")));
    }

    // Each reached a forward on 127.0.0.1 before `destination` made it
    // canonical (alias_probe, 2026-11-18).
    #[test]
    fn an_alias_of_a_forward_address_reaches_no_forward() {
        let outside = outside(&["8080=10.0.2.15:80@/r"]);
        for dst in ["0.0.0.0:8080", "[::ffff:127.0.0.1]:8080"] {
            let mapped = outside.destination(at(dst));
            assert_eq!(mapped, at("127.0.0.1:8080"), "{dst}");
            assert!(!outside.allows(Network::Data, TCP, mapped), "{dst}");
        }
        assert_eq!(outside.destination(at("[::]:8080")), at("[::1]:8080"));
    }
}

//! On the host. Each connection on the listen socket is one flow from
//! the router's relay: a request naming the destination, then the bytes
//! (TCP) or length-prefixed datagrams (UDP, ICMP echo).
//! vhost-device-vsock and libkrun both hand a guest's vsock stream to a
//! unix socket, so this serves either.
//!
//!     egress LISTEN_PATH [GATEWAY [FORWARD...]] [--loopback=ADDR...] [--map=FROM=TO...] [--outbound=ADDR...]
//!
//! A flow to GATEWAY, or to a `--loopback` ADDR, goes to the host's
//! loopback of its family, as passt's `--map-host-loopback`. `--map` sends
//! a flow to the ADDR:PORT FROM to TO instead: the guests' resolver to the
//! upstream one, as `--dns-forward`. `--outbound` binds every outside
//! socket of ADDR's family to ADDR, as passt's: 127.0.0.1 and ::1 reach
//! nothing off this host.
//!
//! FORWARD is `[tcp/|udp/]HOST=GUEST_ADDR:PORT@ROUTER`. HOST is
//! `ADDR:FIRST[-LAST]`, each host port reaching the guest port at the same
//! offset from PORT, or a bare port on 127.0.0.1. A port that will not bind
//! is an error. ROUTER is the path libkrun listens on for the relay, or
//! `vsock:PORT` on the device. Every listener is bound before the device's
//! socket appears.

use std::collections::HashMap;
use std::env;
use std::io::{self, Read, Write};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, TcpListener, TcpStream, UdpSocket};
use std::os::fd::{AsRawFd, OwnedFd, RawFd};
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::{Arc, Mutex};
use std::thread;

#[cfg(all(target_os = "linux", feature = "vhost-user"))]
use vivarium_egress::vsock;

use vivarium_egress::{
    ICMP, REQUEST_LEN, Stream, TCP, UDP, bind, connect, decode, echo_reply, encode, family, owned,
    pump, pump_map, socket, splice_both, widen_unix,
};

/// Where a flow from the router goes on the host's side.
#[derive(Default)]
struct Outside {
    loopback: Vec<IpAddr>,
    maps: Vec<(SocketAddr, SocketAddr)>,
    outbound: Vec<IpAddr>,
}

impl Outside {
    fn destination(&self, mut dst: SocketAddr) -> SocketAddr {
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

fn flow(mut relay: UnixStream, outside: &Outside) -> io::Result<()> {
    widen_unix(&relay)?;
    let mut raw = [0u8; REQUEST_LEN];
    relay.read_exact(&mut raw)?;
    let (proto, dst) = decode(&raw)?;
    let dst = outside.destination(dst);
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
fn device(outside: Arc<Outside>) -> io::Result<Arc<vsock::Device>> {
    // Every host port serves egress flows; the relay picks one.
    let service: vsock::Service = Arc::new(move |relay| {
        if let Err(err) = flow(relay, &outside) {
            eprintln!("egress: {err}");
        }
    });
    vsock::Device::new(GUEST_CID, Default::default(), Some(service))
}

#[cfg(all(target_os = "linux", feature = "vhost-user"))]
const GUEST_CID: u64 = 3;

/// One FORWARD argument.
struct Spec {
    proto: u8,
    bind: IpAddr,
    first: u16,
    last: u16,
    guest: SocketAddr,
    router: Router,
}

fn parse_port(text: &str) -> io::Result<u16> {
    text.parse()
        .map_err(|err| io::Error::other(format!("port {text:?}: {err}")))
}

#[cfg(all(target_os = "linux", feature = "vhost-user"))]
type DeviceRef<'a> = Option<&'a Arc<vsock::Device>>;
#[cfg(not(all(target_os = "linux", feature = "vhost-user")))]
type DeviceRef<'a> = Option<&'a ()>;

fn parse_spec(text: &str, device: DeviceRef) -> io::Result<Spec> {
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
    let router = match router.strip_prefix("vsock:") {
        #[cfg(all(target_os = "linux", feature = "vhost-user"))]
        Some(port) => Router::Vsock(
            device
                .ok_or_else(|| bad("vsock: needs vhost-user:"))?
                .clone(),
            port.parse().map_err(|_| bad("not a vsock port"))?,
        ),
        #[cfg(not(all(target_os = "linux", feature = "vhost-user")))]
        Some(_) => {
            let _ = device;
            return Err(bad("built without vhost-user"));
        }
        None => Router::Path(router.to_owned()),
    };
    Ok(Spec {
        proto,
        bind,
        first,
        last,
        guest,
        router,
    })
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
        for port in spec.first..=spec.last {
            let at = SocketAddr::new(spec.bind, port);
            // std sets SO_REUSEADDR on a TCP listener, as passt does.
            let socket = match spec.proto {
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
            guest.set_port(spec.guest.port() + (port - spec.first));
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
    fn new(listeners: &[Listener]) -> io::Result<Self> {
        #[cfg(target_os = "linux")]
        {
            let epoll = unsafe { owned(libc::epoll_create1(libc::EPOLL_CLOEXEC))? };
            for (index, listener) in listeners.iter().enumerate() {
                let mut event = libc::epoll_event {
                    events: libc::EPOLLIN as u32,
                    u64: index as u64,
                };
                let rc = unsafe {
                    libc::epoll_ctl(
                        epoll.as_raw_fd(),
                        libc::EPOLL_CTL_ADD,
                        listener.fd(),
                        &mut event,
                    )
                };
                if rc != 0 {
                    return Err(io::Error::last_os_error());
                }
            }
            Ok(Self { epoll })
        }
        #[cfg(not(target_os = "linux"))]
        {
            let kqueue = unsafe { owned(libc::kqueue())? };
            let changes: Vec<libc::kevent> = listeners
                .iter()
                .enumerate()
                .map(|(index, listener)| libc::kevent {
                    ident: listener.fd() as libc::uintptr_t,
                    filter: libc::EVFILT_READ,
                    flags: libc::EV_ADD,
                    fflags: 0,
                    data: 0,
                    udata: index as *mut libc::c_void,
                })
                .collect();
            for chunk in changes.chunks(1024) {
                let rc = unsafe {
                    libc::kevent(
                        kqueue.as_raw_fd(),
                        chunk.as_ptr(),
                        chunk.len() as libc::c_int,
                        std::ptr::null_mut(),
                        0,
                        std::ptr::null(),
                    )
                };
                if rc != 0 {
                    return Err(io::Error::last_os_error());
                }
            }
            Ok(Self { kqueue })
        }
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

fn serve(listeners: Vec<Listener>, routers: Vec<Router>) -> io::Result<()> {
    let mut poller = Poller::new(&listeners)?;
    let flows: Flows = Arc::default();
    let mut next_id = 0u64;
    let mut ready = Vec::new();
    let mut buf = vec![0u8; 65536];
    loop {
        poller.wait(&mut ready)?;
        for &index in &ready {
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
    for arg in env::args() {
        if let Some(map) = arg.strip_prefix("--map=") {
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
        io::Error::other("usage: egress LISTEN_PATH|vhost-user:SOCKET [GATEWAY [FORWARD...]] [--loopback=ADDR] [--map=FROM=TO] [--outbound=ADDR]")
    })?;
    if let Some(gateway) = args.get(2) {
        outside
            .loopback
            .push(gateway.parse().map_err(io::Error::other)?);
    }
    let outside = Arc::new(outside);
    // vhost-user:SOCKET serves the router's vsock device on SOCKET.
    #[cfg(all(target_os = "linux", feature = "vhost-user"))]
    let device = match path.strip_prefix("vhost-user:") {
        Some(socket) => Some((device(outside.clone())?, socket.to_owned())),
        None => None,
    };
    #[cfg(all(target_os = "linux", feature = "vhost-user"))]
    let device_ref = device.as_ref().map(|(device, _)| device);
    #[cfg(not(all(target_os = "linux", feature = "vhost-user")))]
    let device_ref = None;
    let specs = args[3.min(args.len())..]
        .iter()
        .map(|spec| parse_spec(spec, device_ref))
        .collect::<io::Result<Vec<_>>>()?;
    if !specs.is_empty() {
        raise_nofile()?;
        let listeners = bind_all(&specs)?;
        let routers = specs.into_iter().map(|spec| spec.router).collect();
        thread::spawn(move || {
            if let Err(err) = serve(listeners, routers) {
                eprintln!("egress: forwards: {err}");
                std::process::exit(1);
            }
        });
    }
    #[cfg(all(target_os = "linux", feature = "vhost-user"))]
    if let Some((device, socket)) = device {
        return device.run(std::path::Path::new(&socket));
    }
    let listener = UnixListener::bind(path)?;
    for relay in listener.incoming() {
        let relay = relay?;
        let outside = outside.clone();
        thread::spawn(move || {
            if let Err(err) = flow(relay, &outside) {
                eprintln!("egress: {err}");
            }
        });
    }
    Ok(())
}

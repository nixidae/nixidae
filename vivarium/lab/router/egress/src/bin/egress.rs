//! On the host. Each connection on the listen socket is one flow from
//! the router's relay: a request naming the destination, then the bytes
//! (TCP) or length-prefixed datagrams (UDP, ICMP echo).
//! vhost-device-vsock and libkrun both hand a guest's vsock stream to a
//! unix socket, so this serves either.
//!
//!     egress LISTEN_PATH [GATEWAY [FORWARD...]]
//!
//! A flow to GATEWAY goes to the host's 127.0.0.1, as passt's
//! `--map-host-loopback`.

use std::env;
use std::io::{self, Read, Write};
use std::net::{IpAddr, TcpListener, TcpStream, UdpSocket};
use std::os::unix::net::{UnixListener, UnixStream};
#[cfg(all(target_os = "linux", feature = "vhost-user"))]
use std::sync::Arc;
use std::thread;

#[cfg(all(target_os = "linux", feature = "vhost-user"))]
use vivarium_egress::vsock;

use vivarium_egress::{
    ICMP, REQUEST_LEN, Stream, TCP, UDP, decode, encode, owned, pump, splice_both,
};

fn flow(mut relay: UnixStream, gateway: Option<IpAddr>) -> io::Result<()> {
    let mut raw = [0u8; REQUEST_LEN];
    relay.read_exact(&mut raw)?;
    let (proto, mut dst) = decode(&raw)?;
    if Some(dst.ip()) == gateway {
        dst.set_ip(IpAddr::from([127, 0, 0, 1]));
    }
    match proto {
        TCP => {}
        UDP => {
            let socket = UdpSocket::bind("0.0.0.0:0")?;
            socket.connect(dst)?;
            return pump(Stream::from(relay), socket);
        }
        ICMP => {
            // A ping socket: no privilege needed where ping_group_range
            // allows it, and on macOS. The kernel picks the echo id.
            let fd = unsafe {
                owned(libc::socket(
                    libc::AF_INET,
                    libc::SOCK_DGRAM | libc::SOCK_CLOEXEC,
                    libc::IPPROTO_ICMP,
                ))?
            };
            let socket = UdpSocket::from(fd);
            socket.connect(dst)?;
            return pump(Stream::from(relay), socket);
        }
        _ => {
            relay.write_all(&[1])?;
            return Err(io::Error::other(format!("protocol {proto} is not served")));
        }
    }
    let outside = match TcpStream::connect(dst) {
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

fn forward(client: TcpStream, guest: std::net::SocketAddr, router: &Router) -> io::Result<()> {
    let mut relay = router.connect()?;
    relay.write_all(&encode(TCP, guest))?;
    let mut status = [0u8; 1];
    relay.read_exact(&mut status)?;
    if status[0] != 0 {
        return Err(io::Error::other("the relay could not connect"));
    }
    splice_both(Stream::from(client), Stream::from(relay))
}

#[cfg(all(target_os = "linux", feature = "vhost-user"))]
fn device(gateway: Option<IpAddr>) -> io::Result<Arc<vsock::Device>> {
    // Every host port serves egress flows; the relay picks one.
    let service: vsock::Service = Arc::new(move |relay| {
        if let Err(err) = flow(relay, gateway) {
            eprintln!("egress: {err}");
        }
    });
    vsock::Device::new(GUEST_CID, Default::default(), Some(service))
}

#[cfg(all(target_os = "linux", feature = "vhost-user"))]
const GUEST_CID: u64 = 3;

fn main() -> io::Result<()> {
    let args: Vec<String> = env::args().collect();
    let path = args.get(1).ok_or_else(|| {
        io::Error::other("usage: egress LISTEN_PATH|vhost-user:SOCKET [GATEWAY [FORWARD...]]")
    })?;
    let gateway: Option<IpAddr> = args
        .get(2)
        .map(|a| a.parse())
        .transpose()
        .map_err(io::Error::other)?;
    // vhost-user:SOCKET serves the router's vsock device on SOCKET.
    #[cfg(all(target_os = "linux", feature = "vhost-user"))]
    let device = match path.strip_prefix("vhost-user:") {
        Some(socket) => Some((device(gateway)?, socket.to_owned())),
        None => None,
    };
    // FORWARD: HOST_PORT=GUEST_ADDR:PORT@ROUTER, where ROUTER is the
    // path libkrun listens on for the relay, or vsock:PORT on the device.
    for spec in &args[3.min(args.len())..] {
        let (port, rest) = spec
            .split_once('=')
            .ok_or_else(|| io::Error::other("forward: no ="))?;
        let (guest, router) = rest
            .split_once('@')
            .ok_or_else(|| io::Error::other("forward: no @"))?;
        let guest: std::net::SocketAddr = guest.parse().map_err(io::Error::other)?;
        let outside =
            TcpListener::bind(("127.0.0.1", port.parse::<u16>().map_err(io::Error::other)?))?;
        let router = match router.strip_prefix("vsock:") {
            #[cfg(all(target_os = "linux", feature = "vhost-user"))]
            Some(port) => {
                let (device, _) = device
                    .as_ref()
                    .ok_or_else(|| io::Error::other("forward: vsock: needs vhost-user:"))?;
                Router::Vsock(device.clone(), port.parse().map_err(io::Error::other)?)
            }
            #[cfg(not(all(target_os = "linux", feature = "vhost-user")))]
            Some(_) => return Err(io::Error::other("forward: built without vhost-user")),
            None => Router::Path(router.to_owned()),
        };
        thread::spawn(move || {
            for client in outside.incoming().flatten() {
                let router = router.clone();
                thread::spawn(move || {
                    if let Err(err) = forward(client, guest, &router) {
                        eprintln!("egress: forward {guest}: {err}");
                    }
                });
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
        thread::spawn(move || {
            if let Err(err) = flow(relay, gateway) {
                eprintln!("egress: {err}");
            }
        });
    }
    Ok(())
}

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
use std::thread;

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

fn forward(client: TcpStream, guest: std::net::SocketAddr, router: &str) -> io::Result<()> {
    let mut relay = UnixStream::connect(router)?;
    relay.write_all(&encode(TCP, guest))?;
    let mut status = [0u8; 1];
    relay.read_exact(&mut status)?;
    if status[0] != 0 {
        return Err(io::Error::other("the relay could not connect"));
    }
    splice_both(Stream::from(client), Stream::from(relay))
}

fn main() -> io::Result<()> {
    let args: Vec<String> = env::args().collect();
    let path = args
        .get(1)
        .ok_or_else(|| io::Error::other("usage: egress LISTEN_PATH [GATEWAY]"))?;
    let gateway: Option<IpAddr> = args
        .get(2)
        .map(|a| a.parse())
        .transpose()
        .map_err(io::Error::other)?;
    // FORWARD: HOST_PORT=GUEST_ADDR:PORT@ROUTER_PATH, where the router
    // listens on ROUTER_PATH for connections to its relay.
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
        let router = router.to_owned();
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

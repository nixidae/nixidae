//! On the host. Each connection on the listen socket is one flow from
//! the router's relay: a request naming the destination, then the bytes.
//! vhost-device-vsock and libkrun both hand a guest's vsock stream to a
//! unix socket, so this serves either.
//!
//!     egress LISTEN_PATH [GATEWAY]
//!
//! A flow to GATEWAY goes to the host's 127.0.0.1, as passt's
//! `--map-host-loopback`.

use std::env;
use std::io::{self, Read, Write};
use std::net::{IpAddr, TcpStream};
use std::os::unix::net::{UnixListener, UnixStream};
use std::thread;

use vivarium_egress::{REQUEST_LEN, Stream, TCP, decode, splice_both};

fn flow(mut relay: UnixStream, gateway: Option<IpAddr>) -> io::Result<()> {
    let mut raw = [0u8; REQUEST_LEN];
    relay.read_exact(&mut raw)?;
    let (proto, mut dst) = decode(&raw)?;
    if proto != TCP {
        relay.write_all(&[1])?;
        return Err(io::Error::other(format!("protocol {proto} is not served yet")));
    }
    if Some(dst.ip()) == gateway {
        dst.set_ip(IpAddr::from([127, 0, 0, 1]));
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

fn main() -> io::Result<()> {
    let args: Vec<String> = env::args().collect();
    let path = args.get(1).ok_or_else(|| io::Error::other("usage: egress LISTEN_PATH [GATEWAY]"))?;
    let gateway: Option<IpAddr> = args.get(2).map(|a| a.parse()).transpose().map_err(io::Error::other)?;
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

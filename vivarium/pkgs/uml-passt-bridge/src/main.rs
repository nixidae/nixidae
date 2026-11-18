//! Bridge between UML fd vector transport and passt.
//!
//! Creates two socketpairs, forks passt (connected to fd 4) and UML
//! (connected to fd 3), then bridges Ethernet frames between them.
//! UML side: raw Ethernet frames.
//! Passt side: 4-byte big-endian length prefix + Ethernet frame.
//!
//! Usage: uml-passt-bridge UML_BINARY [UML_ARGS...]

use std::{
    env, io,
    os::fd::{AsRawFd, BorrowedFd},
    os::unix::process::CommandExt,
    process::{self, exit},
    sync::atomic::{AtomicBool, Ordering},
};

use nix::{
    fcntl,
    sys::{
        signal::{self, kill, Signal},
        socket::{self, AddressFamily, SockType, SockFlag},
        wait::{waitpid, WaitPidFlag, WaitStatus},
    },
    unistd::{self, ForkResult},
};

const POLLIN: i16 = 0x001;
const POLLERR: i16 = 0x008;
const POLLHUP: i16 = 0x010;
const UML_FD: i32 = 3;
const PASST_FD: i32 = 4;

static SHUTDOWN: AtomicBool = AtomicBool::new(false);

extern "C" fn handle_term(_sig: i32) {
    SHUTDOWN.store(true, Ordering::SeqCst);
}

/// SIGKILL this forked child when the bridge dies. A fork clears the
/// parent-death signal, and a SIGKILLed bridge runs no cleanup. The
/// `getppid` check closes the race where the bridge died before the prctl.
fn die_with_bridge(bridge: unistd::Pid) {
    unsafe {
        libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
    }
    if unistd::getppid() != bridge {
        exit(1);
    }
}

fn read_exact(fd: i32, buf: &mut [u8]) -> io::Result<()> {
    let mut off = 0;
    while off < buf.len() {
        match unistd::read(fd, &mut buf[off..]) {
            Ok(0) => return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "eof")),
            Ok(n) => off += n,
            Err(e) => return Err(e.into()),
        }
    }
    Ok(())
}

/// What passt and the kernel are started with.
#[derive(Debug, PartialEq)]
struct Command {
    passt: Vec<String>,
    kernel: String,
    kernel_args: Vec<String>,
}

/// The command line, after the program name: the bridge's own flags, then
/// the kernel and its arguments.
fn parse(args: &[String]) -> Result<Command, String> {
    // Passed through to passt's -t and -u verbatim, so they carry a whole
    // port specifier -- an address, ranges, exclusions -- not just a port.
    // The runner builds them; see vivarium_runner/forward.py.
    let mut tcp_ports: Vec<String> = Vec::new();
    let mut udp_ports: Vec<String> = Vec::new();
    // Anything else passt should be told, one argument per flag, appended
    // verbatim. The runner owns the uplink's addressing for both backends
    // -- see `uplink_args` in vivarium_runner/forward.py -- and this is how
    // it reaches passt on the side that has a bridge in the way.
    let mut passt_extra: Vec<String> = Vec::new();
    let mut vec_arg = "vec0:transport=fd,fd=3,depth=512,gro=1".to_string();

    let mut rest = args.iter();
    let kernel = loop {
        let Some(flag) = rest.next() else {
            return Err("Usage: uml-passt-bridge [--vec VEC_ARG] [--tcp-ports SPEC]... \
                        [--udp-ports SPEC]... [--passt ARG]... UML_BINARY [UML_ARGS...]"
                .into());
        };
        if !matches!(flag.as_str(), "--vec" | "--tcp-ports" | "--udp-ports" | "--passt") {
            break flag.clone();
        }
        let value = rest
            .next()
            .ok_or_else(|| format!("{flag} requires an argument"))?
            .clone();
        match flag.as_str() {
            "--vec" => vec_arg = value,
            "--tcp-ports" => tcp_ports.push(value),
            "--udp-ports" => udp_ports.push(value),
            _ => passt_extra.push(value),
        }
    };

    // No --one-off: we kill passt ourselves when the bridge exits, and
    // letting it linger keeps the uplink up across a guest's reboot.
    let mut passt: Vec<String> = vec!["--foreground".into(), "--fd".into(), "4".into()];
    passt.extend(passt_extra);
    for (flag, specs) in [("-t", tcp_ports), ("-u", udp_ports)] {
        for spec in specs {
            passt.push(flag.into());
            passt.push(spec);
        }
    }
    let mut kernel_args: Vec<String> = rest.cloned().collect();
    kernel_args.push(vec_arg);
    Ok(Command {
        passt,
        kernel,
        kernel_args,
    })
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    let command = parse(&args).unwrap_or_else(|why| {
        eprintln!("{why}");
        exit(1);
    });

    let (uml_a, uml_b) = socket::socketpair(
        AddressFamily::Unix,
        SockType::SeqPacket,
        None,
        SockFlag::empty(),
    )
    .expect("socketpair uml");
    let (passt_a, passt_b) = socket::socketpair(
        AddressFamily::Unix,
        SockType::Stream,
        None,
        SockFlag::empty(),
    )
    .expect("socketpair passt");

    let bridge = unistd::getpid();
    let passt_pid = match unsafe { unistd::fork() }.expect("fork passt") {
        ForkResult::Child => {
            die_with_bridge(bridge);
            drop(uml_a);
            drop(uml_b);
            drop(passt_a);
            unistd::dup2(passt_b.as_raw_fd(), PASST_FD).expect("dup2 passt");
            drop(passt_b);
            let err = process::Command::new("passt").args(&command.passt).exec();
            eprintln!("exec passt: {}", err);
            exit(1);
        }
        ForkResult::Parent { child } => child,
    };

    let uml_pid = match unsafe { unistd::fork() }.expect("fork uml") {
        ForkResult::Child => {
            die_with_bridge(bridge);
            drop(passt_a);
            drop(passt_b);
            drop(uml_a);
            unistd::dup2(uml_b.as_raw_fd(), UML_FD).expect("dup2 uml");
            drop(uml_b);
            fcntl::fcntl(UML_FD, fcntl::FcntlArg::F_SETFD(fcntl::FdFlag::empty()))
                .expect("fcntl uml");

            let err = process::Command::new(&command.kernel)
                .args(&command.kernel_args)
                .exec();
            eprintln!("exec uml {}: {}", command.kernel, err);
            exit(1);
        }
        ForkResult::Parent { child } => child,
    };

    // Parent: keep both ends alive so socketpairs survive child exits.
    let uml_r = uml_a.as_raw_fd();
    let passt_r = passt_a.as_raw_fd();
    let _uml_b = uml_b;
    let _passt_b = passt_b;

    unsafe {
        signal::signal(Signal::SIGTERM, signal::SigHandler::Handler(handle_term)).ok();
        signal::signal(Signal::SIGINT, signal::SigHandler::Handler(handle_term)).ok();
        signal::signal(Signal::SIGHUP, signal::SigHandler::Handler(handle_term)).ok();
        signal::signal(Signal::SIGPIPE, signal::SigHandler::SigIgn).ok();
        libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM);
    }

    let mut pfds = [
        libc::pollfd { fd: uml_r, events: POLLIN, revents: 0 },
        libc::pollfd { fd: passt_r, events: POLLIN, revents: 0 },
    ];

    let mut buf = vec![0u8; 65536];
    let mut framed = vec![0u8; 65540];
    let mut status = 0;

    loop {
        if SHUTDOWN.load(Ordering::SeqCst) {
            break;
        }

        let ret = unsafe { libc::poll(pfds.as_mut_ptr(), pfds.len() as libc::nfds_t, 1000) };
        if ret < 0 {
            let e = io::Error::last_os_error();
            if e.raw_os_error() == Some(libc::EINTR) {
                continue;
            }
            eprintln!("poll error: {}", e);
            break;
        }

        // Ask whether passt is still there, because nothing else will
        // say.  The parent holds passt's end of the socketpair open --
        // see above -- so passt can exit without ever giving this loop
        // an EOF to notice, and the guest then runs on with an uplink
        // that answers nothing.  passt does exit outright: a port it was
        // told to bind and cannot, or a user namespace it cannot
        // unshare, and it is gone before the guest has finished booting.
        match waitpid(passt_pid, Some(WaitPidFlag::WNOHANG)) {
            Ok(WaitStatus::Exited(_, code)) => {
                eprintln!("passt exited ({}); the guest has no uplink", code);
                break;
            }
            Ok(WaitStatus::Signaled(_, sig, _)) => {
                eprintln!("passt killed by {}; the guest has no uplink", sig);
                break;
            }
            _ => {}
        }

        // And the kernel, for the same reason: this process holds its end
        // of the socketpair too, so no EOF says it is gone. Measured with
        // `uml run --kernel` naming a file that is not a kernel: the exec
        // failed, the bridge ran on, and the runner -- whose child is this
        // bridge -- waited out its whole boot timeout for a guest that
        // never existed. A panic or a poweroff is the same case later on.
        // Exiting with the kernel's status is what tells the runner.
        match waitpid(uml_pid, Some(WaitPidFlag::WNOHANG)) {
            Ok(WaitStatus::Exited(_, code)) => {
                eprintln!("the kernel exited ({})", code);
                status = code;
                break;
            }
            Ok(WaitStatus::Signaled(_, sig, _)) => {
                eprintln!("the kernel was killed by {}", sig);
                status = 128 + sig as i32;
                break;
            }
            _ => {}
        }

        // UML -> passt: read raw frame, prepend 4-byte BE length, forward.
        if pfds[0].revents & (POLLIN | POLLERR | POLLHUP) != 0 {
            if pfds[0].revents & (POLLHUP | POLLERR) != 0 {
                break;
            }
            match unistd::read(uml_r, &mut buf) {
                Ok(n) if n > 0 => {
                    let len_be = (n as u32).to_be_bytes();
                    framed[..4].copy_from_slice(&len_be);
                    framed[4..4 + n].copy_from_slice(&buf[..n]);
                    let bfd = unsafe { BorrowedFd::borrow_raw(passt_r) };
                    match unistd::write(&bfd, &framed[..4 + n]) {
                        Ok(_) => {}
                        Err(e) => {
                            eprintln!("passt write error: {}", e);
                            break;
                        }
                    }
                }
                _ => {
                    eprintln!("uml read eof");
                    break;
                }
            }
        }

        // Passt -> UML: read 4-byte BE length, read payload, forward raw.
        if pfds[1].revents & (POLLIN | POLLERR | POLLHUP) != 0 {
            if pfds[1].revents & (POLLHUP | POLLERR) != 0 {
                break;
            }
            let mut len_be = [0u8; 4];
            if read_exact(passt_r, &mut len_be).is_err() {
                eprintln!("passt read header eof");
                break;
            }
            let len = u32::from_be_bytes(len_be) as usize;
            if len > buf.len() {
                eprintln!("passt frame too large: {}", len);
                break;
            }
            if read_exact(passt_r, &mut buf[..len]).is_err() {
                eprintln!("passt read payload eof");
                break;
            }
            let bfd = unsafe { BorrowedFd::borrow_raw(uml_r) };
            match unistd::write(&bfd, &buf[..len]) {
                Ok(_) => {}
                Err(e) => {
                    eprintln!("uml write error: {}", e);
                    break;
                }
            }
        }
    }

    // Kill children so nothing leaks.
    let _ = kill(passt_pid, Signal::SIGKILL);
    let _ = kill(uml_pid, Signal::SIGKILL);
    exit(status);
}

#[cfg(test)]
mod tests {
    use super::{Command, parse};

    fn words(line: &str) -> Vec<String> {
        line.split_whitespace().map(String::from).collect()
    }

    // What `forward.to_args` and `uplink_args` write ahead of the kernel.
    #[test]
    fn passt_gets_its_options_then_each_port_spec() {
        let command = parse(&words(
            "--tcp-ports 127.0.0.3/8080,14325:22 --passt --dns-forward --passt 10.0.2.3 \
             --udp-ports 127.0.0.3/53 --tcp-ports 127.0.0.3/6443 /linux mem=512M",
        ))
        .unwrap();
        assert_eq!(
            command,
            Command {
                passt: words(
                    "--foreground --fd 4 --dns-forward 10.0.2.3 -t 127.0.0.3/8080,14325:22 \
                     -t 127.0.0.3/6443 -u 127.0.0.3/53"
                ),
                kernel: "/linux".into(),
                kernel_args: words("mem=512M vec0:transport=fd,fd=3,depth=512,gro=1"),
            }
        );
    }

    #[test]
    fn the_vec_argument_replaces_the_default_and_a_flag_after_the_kernel_is_its() {
        let command = parse(&words("--vec vec0:transport=fd,fd=3 /linux --vec x")).unwrap();
        assert_eq!(command.passt, words("--foreground --fd 4"));
        assert_eq!(command.kernel_args, words("--vec x vec0:transport=fd,fd=3"));
    }

    #[test]
    fn a_flag_without_its_value_or_no_kernel_is_refused() {
        assert_eq!(
            parse(&words("--tcp-ports")),
            Err("--tcp-ports requires an argument".into())
        );
        assert!(parse(&words("--tcp-ports 127.0.0.3/22")).unwrap_err().starts_with("Usage:"));
    }
}

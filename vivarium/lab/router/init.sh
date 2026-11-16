#!/bin/sh
# The lab guests' /init: no Python in this root, so busybox sh.
# `lab=` on the kernel line picks the experiment or the role.
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
arg() { sed -n "s/.*$1=\([a-z0-9.-]*\).*/\1/p" /proc/cmdline; }
lab=$(arg lab)
block=$(arg block)
block=${block:-8192}
echo "lab: up, lab=$lab block=$block"
ip link set lo up
krun=$(arg krun)
if [ -n "$krun" ]; then
  # E2: the stock kernel has the devices and nftables as modules, and
  # names the NIC eth0. The labs below say vec0, as on UML.
  for module in virtio_mmio virtio_net vmw_vsock_virtio_transport nft_chain_nat nft_redir nft_nat nf_tables nft_tproxy nft_socket nft_queue; do
    modprobe "$module"
  done
  ip link set eth0 name vec0
fi
mtu=$(arg mtu)
[ -n "$mtu" ] && ip link set vec0 mtu "$mtu"
case "$lab" in
  vsock-tx)
    # E1: stream zeros to the host's vsock port 5000.
    dd if=/dev/zero bs=1M count=4096 2>/dev/null | socat -b "$block" -u - VSOCK-CONNECT:2:5000
    echo "lab: vsock-tx done"
    ;;
  local)
    # E1 baseline: the same pipe with no vsock.
    start=$(date +%s)
    dd if=/dev/zero bs=1M count=4096 2>/dev/null | socat -b "$block" -u - /dev/null
    echo "lab: local done in $(($(date +%s) - start)) s"
    ;;
  router)
    # E4: the router. vec0 faces the client; the host is over vsock.
    ip addr add 10.0.2.1/24 dev vec0
    # The host's address answers here too; the relay maps it to the
    # host's loopback.
    ip addr add 10.0.2.2/24 dev vec0
    ip link set vec0 up
    echo 1 > /proc/sys/net/ipv4/ip_forward
    # The relay answers echo requests with the host's replies.
    echo 1 > /proc/sys/net/ipv4/icmp_echo_ignore_all
    ip rule add fwmark 1 lookup 100
    ip route add local 0.0.0.0/0 dev lo table 100
    nft -f /etc/router.nft
    # E5's negative control: no mark, so ICMP echo is routed, not relayed.
    [ -n "$(arg noicmp)" ] && nft flush chain ip router mangle
    relay 9000 5000 9001 5001 &
    echo "lab: router ready"
    # Runs until the host kills it. The guest's own CPU counters: on KVM
    # the host's per-thread time includes halt polling.
    while true; do read -r stat < /proc/stat; echo "lab: stat $stat"; sleep 2; done
    ;;
  client)
    # E4: a guest behind the router. 10.0.2.2 is the host's loopback.
    ip addr add 10.0.2.15/24 dev vec0
    ip link set vec0 up
    ip route add default via 10.0.2.1
    sleep 1
    streams=$(arg streams)
    iperf3 -c 10.0.2.2 -t 10 -l "$block" -P "${streams:-1}" $(arg reverse | sed 's/1/-R/')
    echo "lab: client done"
    ;;
  server)
    # E6: a guest that serves; the host reaches it through a forward.
    ip addr add 10.0.2.15/24 dev vec0
    ip link set vec0 up
    ip route add default via 10.0.2.1
    echo "lab: server ready"
    iperf3 -s -1
    echo "lab: client done"
    ;;
  verify)
    # E7: content, not only byte counts. seq output is not uniform, so
    # reordered or repeated data changes the sum.
    ip addr add 10.0.2.15/24 dev vec0
    ip link set vec0 up
    ip route add default via 10.0.2.1
    sleep 1
    seq 1 "$(arg count)" | socat -b 131072 -u - TCP:10.0.2.2:7000 && echo "lab: verify sent"
    echo "lab: verify got $(socat -b 131072 -u TCP:10.0.2.2:7001 - | md5sum)"
    echo "lab: client done"
    ;;
  e5)
    # E5: ICMP echo and UDP behind the router. 192.0.2.1 is TEST-NET-1:
    # nothing answers it, so a reply there would be one the router made up.
    ip addr add 10.0.2.15/24 dev vec0
    ip link set vec0 up
    ip route add default via 10.0.2.1
    sleep 1
    ping -c 3 -W 2 10.0.2.2 && echo "lab: e5 ping host ok" || echo "lab: e5 ping host failed"
    ping -c 2 -W 2 192.0.2.1 && echo "lab: e5 ping test-net answered" || echo "lab: e5 ping test-net silent"
    # The host's own address is outside the router: only the mark gets a
    # reply for it (10.0.2.2 is the router's, so its raw socket sees that
    # echo with or without the mark).
    outside=$(arg outside)
    ping -c 2 -W 2 "$outside" && echo "lab: e5 ping outside ok" || echo "lab: e5 ping outside failed"
    reply=$(echo e5-udp | socat -t 2 - UDP:10.0.2.2:7777)
    echo "lab: e5 udp echo: '$reply'"
    rate=$(arg rate)
    ulen=$(arg ulen)
    iperf3 -u -c 10.0.2.2 -b "${rate:-1g}" -t 5 -l "${ulen:-1400}" $(arg reverse | sed 's/1/-R/')
    echo "lab: client done"
    ;;
  *)
    echo "lab: no lab"
    ;;
esac
# libkrun ends on a reset (reboot=k); UML would boot again on one.
[ -n "$krun" ] && sleep 1 && reboot -f
poweroff -f

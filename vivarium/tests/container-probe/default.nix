# Can this sandbox run a container guest? Seconds, and every missing
# piece named with its fix. `-tun` also asks for a tap device, which a
# LAN needs: /dev/net in `extra-sandbox-paths`.
{ vivarium, ... }:
vivarium.containerProbe { }

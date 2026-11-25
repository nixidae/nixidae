# Can this sandbox run a container guest? Seconds, and every missing
# piece named with its fix. `-lan` also asks for a veth pair, which a
# LAN needs.
{ vivarium, ... }:
vivarium.containerProbe { }

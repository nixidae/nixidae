# The lab's PKI, made with strongSwan's own `pki` tool at build time.
#
# Private keys land in the store. That is right for a throwaway test PKI
# and wrong for anything else.
#
#   ca          the trust anchor every gateway loads
#   ca.crl      revokes serial 03, valid for ten years
#   stale.crl   the same revocation, but its nextUpdate passed in 2021
#   west, east  the two gateways; IKE identity = the SAN
#   revoked     a second certificate for west's identity, revoked for key
#               compromise: the stolen-key case
#   renewed     west's identity with a new key: the planned rotation
#   expired     west's identity, valid only during 2020
#   rogue-ca    a CA nobody trusts
#   rogue       west's identity again, signed by rogue-ca: the impostor case
{ runCommand, strongswan }:
runCommand "ipsec-lab-pki" { nativeBuildInputs = [ strongswan ]; } ''
  mkdir -p $out
  cd $out

  # pki reads /etc/strongswan.conf, which the sandbox lacks, and aborts.
  export STRONGSWAN_CONF=$TMPDIR/strongswan.conf
  touch "$STRONGSWAN_CONF"

  key() { pki --gen --type ecdsa --size 384 --outform pem > "$1.key"; }

  ca() {
    key "$1"
    pki --self --ca --lifetime 3650 --in "$1.key" --type ecdsa \
      --dn "C=SE, O=vivarium, CN=$2" --outform pem > "$1.pem"
  }

  past="--dateform %Y-%m-%d --not-before 2020-01-01 --not-after 2021-01-01"

  # issue NAME CA SERIAL FQDN [VALIDITY]
  issue() {
    key "$1"
    pki --pub --in "$1.key" --type ecdsa \
      | pki --issue --cacert "$2.pem" --cakey "$2.key" --serial "$3" \
          ''${5:---lifetime 730} --dn "C=SE, O=vivarium, CN=$4" --san "$4" \
          --flag serverAuth --flag ikeIntermediate --outform pem > "$1.pem"
  }

  ca ca "vivarium IPsec lab CA"
  issue west ca 01 west.vpn.test
  issue east ca 02 east.vpn.test
  issue revoked ca 03 west.vpn.test
  issue renewed ca 04 west.vpn.test
  issue expired ca 05 west.vpn.test "$past"

  ca rogue-ca "rogue CA"
  issue rogue rogue-ca 01 west.vpn.test

  pki --signcrl --cacert ca.pem --cakey ca.key --lifetime 3650 \
    --reason key-compromise --serial 03 --outform pem > ca.crl
  pki --signcrl --cacert ca.pem --cakey ca.key \
    --dateform %Y-%m-%d --this-update 2020-01-01 --next-update 2021-01-01 \
    --reason key-compromise --serial 03 --outform pem > stale.crl

  # The build fails here, not in a guest, if the chain is not what the
  # test assumes.
  for good in west east renewed; do
    pki --verify --in $good.pem --cacert ca.pem
  done
  for bad in rogue expired; do
    if pki --verify --in $bad.pem --cacert ca.pem; then
      echo "$bad.pem verifies against the lab CA" >&2
      exit 1
    fi
  done
  for crl in ca stale; do
    pki --print --type crl --in $crl.crl | grep -q "03.*key compromise" || {
      pki --print --type crl --in $crl.crl >&2
      exit 1
    }
  done
''

#!/bin/bash
# Vendored from boxd-sh/boxd scripts/boxd-guest-fw.sh at
# 7dc2387ef85f2aa06faa7e64b6b82af302a5b969, unchanged below this note except that
# references to boxd-internal tickets and sources are taken out of its
# comments. Update it from there; do not edit it here.
#
# Guest network firewall for a worker host.
#
# Guests share the bridge with infrastructure: the worker is their gateway,
# and proxies and peer workers hold on-link addresses (10.x.0.25x) on every
# worker's bridge subnet through vxlan. This ruleset filters everything a guest
# SENDS, keyed on its tap, so a guest reaches nothing but:
#
#   - the gateway's guest services (DNS 53, metadata 9002, vm_proxy 9003,
#     egress 3128) and ping;
#   - the relay for private services (3129) on its own gateway, only as a
#     connection the worker's nft rule rewrote there;
#   - other guests (same bridge, or another worker's guest range);
#   - a peer worker's gateway on the same ports (a VM restored from another
#     worker's snapshot still dials it);
#   - the proxies' device ingress, tcp 9460, on their on-link addresses;
#   - the internet;
#   - replies on any flow someone else opened to it (conntrack).
#
# Everything else is dropped: the control network, every 10.x.0.y
# infrastructure address, other private ranges, any other port or address of
# this host, IPv6, and non-IP/ARP frames.
#
# Two tables, applied together as one atomic `nft -f` batch (safe to rerun):
#
#   bridge boxd_guest  prerouting: marks every frame that enters from a tap
#                      (MARK below) and drops non-IPv4/ARP.
#                      forward: filters marked frames switched to another
#                      port: other taps, vxlan (proxy, peers), the veth to
#                      the other bridge. The mark survives the veth, so a
#                      boxd-br1 guest's frame is filtered again on boxd-br0.
#   inet boxd_guest    input / forward: filters marked packets the host
#                      receives or routes. Runs after defragmentation and
#                      after the nat hooks (legacy-gateway DNAT, egress
#                      REDIRECT), so it sees the final destination.
#
# The tap is the only guest identity. `iif boxd-br0` is not: proxy and peer
# traffic also arrives on the bridge, through vxlan.
#
# br_netfilter: when it is loaded and net.bridge.bridge-nf-call-iptables is 1
# (a kernel with CONFIG_BRIDGE_NETFILTER=y, or Docker loading the module),
# BRIDGED IPv4 frames also traverse the inet forward hook, with iif/oif set to
# the bridge itself. The inet forward chain therefore never applies routed-only
# rules to a packet whose output is a bridge: those get the same on-link policy
# as the bridge forward chain, and boxd-br1 (no address, never routes) is left
# to the bridge table. The ruleset is correct with the sysctl at 0 or 1.
#
# Routed workers (--routed, plans/2026-10-27-routed-network.md) have no bridge:
# each tap carries the routed datapath's eBPF program, which answers ARP, drops
# spoofed and non-IPv4 frames and sends machine-to-machine traffic straight to
# the other tap or over Geneve. Only what a guest sends to the gateway, the
# internet, a host or a bridge worker's machine reaches the host stack, so the
# routed ruleset is the inet table alone, keyed on `iifname "tap-*"`, plus a nat
# chain that DNATs every old gateway (--legacy-gateway, and each --peer-subnet's
# .0.1) to this worker's: a restored guest's takeoff dials the gateway its frozen
# kernel command line names. --subnet is the cluster's pool, --proxy the
# proxies' host addresses in it (x.y.0.254 downwards), --peer-subnet the bridge
# workers' /16s while they exist.
#
# Requires nf_tables bridge support and bridge conntrack (CONFIG_NF_TABLES_BRIDGE,
# CONFIG_NF_CONNTRACK_BRIDGE). The worker host kernel has both. The guest
# kernel a devbox runs did not, as of 2026-10-01.
#
# Usage:
#   boxd-guest-fw.sh --subnet 10.31.0.0/16 --gateway 10.31.0.1 \
#       [--peer-subnet 10.33.0.0/16]... [--proxy 10.31.0.254]... \
#       [--host-tcp-port 443]... [--print | --check | --remove] [--best-effort]
#   boxd-guest-fw.sh --verify
#   boxd-guest-fw.sh --metrics FILE
#
#   --subnet          this worker's bridge subnet (must be a /16 in 10.0.0.0/8)
#   --gateway         this worker's gateway address on the bridge
#   --peer-subnet     another worker's bridge subnet (repeat per peer)
#   --proxy           a proxy's on-link address in --subnet (repeat per proxy)
#   --host-tcp-port   extra tcp port guests may reach on any address of this
#                     host (repeat). For a devbox, whose proxy is local.
#   --print           print the ruleset, change nothing
#   --check           validate the ruleset against the kernel (nft -c)
#   --remove          delete both tables
#   --verify          exit 0 only if both tables are loaded (boxd-worker's
#                     ExecStartPre: the worker does not start unfiltered)
#   --metrics FILE    write Prometheus text (tables loaded, guest frames seen,
#                     drops per rule) atomically to FILE, for a node exporter
#                     textfile collector. Read-only; exits 0 even when the
#                     tables are missing (that is what it reports).
#   --best-effort     if the kernel lacks bridge nf_tables, warn and exit 0
#                     instead of failing (for `just dev-net`, never workers)
#   --routed          a routed worker: the inet table only (with --verify and
#                     --metrics too)
#   --legacy-gateway  an old gateway to DNAT to --gateway (repeat), --routed only
#   --relay-user NAME the relay for private services runs as NAME:
#                     its own connections are limited to --private-service and DNS.
#                     With --verify: also require that chain
#   --private-service CIDR:PORT  a private service's target (a fixed target is a
#                     /32) and port the relay user may connect to (repeat);
#                     --relay-user only
#   --routed-pool     a bridge worker in a cluster with routed workers: their
#                     machines' pool (10.N.0.0/16), reached through the transit
#                     route; its x.y.0.0/24 (gateway, hosts) stays dropped
set -euo pipefail

MARK=0x10000000                 # unused by anything else on the hosts
TAP_GLOB='tap-*'                # boxd-worker names taps this way
BR0=boxd-br0
BR1=boxd-br1                    # second bridge, no address of its own
BR1_VETH=veth-br10              # boxd-br1's end of the bridge join
GW_TCP_PORTS='53, 9002, 9003, 3128'
# The relay for private services (boxd's RELAY_PORT).
# Not in GW_TCP_PORTS: that list also opens peer gateways, and a box reaches only its own relay.
RELAY_PORT=3129
GW_UDP_PORTS='53'
DEVICE_INGRESS_PORT=9460        # boxd-proxy device ingress
# Never routed to the internet. 224.0.0.0/3 = multicast + reserved.
NONPUBLIC='0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/24, 192.168.0.0/16, 198.18.0.0/15, 224.0.0.0/3'

die() { echo "boxd-guest-fw: $*" >&2; exit 1; }

is_ipv4() {
    local IFS=. a b c d
    [[ "$1" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || return 1
    read -r a b c d <<<"$1"
    (( a <= 255 && b <= 255 && c <= 255 && d <= 255 ))
}

# A worker bridge subnet: 10.N.0.0/16. Guests are allocated only outside
# 10.N.0.0/24 for a /16 (boxd assign_vm_ip); a
# longer prefix puts guests in infrastructure space, so refuse it.
check_subnet() {
    [[ "$1" =~ ^10\.([0-9]{1,3})\.0\.0/16$ ]] && (( BASH_REMATCH[1] <= 255 )) \
        || die "$2 must be a 10.N.0.0/16 bridge subnet, got '$1'"
}
prefix16() { local s=${1%/16}; echo "${s%.0.0}"; }   # 10.31.0.0/16 -> 10.31

SUBNET='' GATEWAY='' MODE=apply BEST_EFFORT=0 ROUTED=0 ROUTED_POOL=''
PEERS=() PROXIES=() HOST_PORTS=() LEGACY_GWS=() RELAY_USER='' PRIVATE_SERVICES=()
while [ $# -gt 0 ]; do
    case "$1" in
        --subnet)        SUBNET=${2:?}; shift 2 ;;
        --gateway)       GATEWAY=${2:?}; shift 2 ;;
        --peer-subnet)   PEERS+=("${2:?}"); shift 2 ;;
        --proxy)         PROXIES+=("${2:?}"); shift 2 ;;
        --host-tcp-port) HOST_PORTS+=("${2:?}"); shift 2 ;;
        --print)         MODE=print; shift ;;
        --check)         MODE=check; shift ;;
        --remove)        MODE=remove; shift ;;
        --verify)        MODE=verify; shift ;;
        --metrics)       MODE=metrics; METRICS_FILE=${2:?}; shift 2 ;;
        --best-effort)   BEST_EFFORT=1; shift ;;
        --routed)        ROUTED=1; shift ;;
        --legacy-gateway) LEGACY_GWS+=("${2:?}"); shift 2 ;;
        --routed-pool)   ROUTED_POOL=${2:?}; shift 2 ;;
        --relay-user)    RELAY_USER=${2:?}; shift 2 ;;
        --private-service)       PRIVATE_SERVICES+=("${2:?}"); shift 2 ;;
        -h|--help)       sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
        *)               die "unknown argument '$1' (see --help)" ;;
    esac
done

remove_batch() {
    printf 'table bridge boxd_guest {}\ndelete table bridge boxd_guest\n'
    printf 'table inet boxd_guest {}\ndelete table inet boxd_guest\n'
}

TABLES=("bridge boxd_guest" "inet boxd_guest")
[ "$ROUTED" = 1 ] && TABLES=("inet boxd_guest")

if [ -n "$RELAY_USER" ]; then
    [[ "$RELAY_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "--relay-user must be a user name, got '$RELAY_USER'"
fi

if [ "$MODE" = verify ]; then
    for t in "${TABLES[@]}"; do
        # shellcheck disable=SC2086
        nft list table $t >/dev/null 2>&1 || die "table $t is not loaded: guests would run unfiltered (systemctl status boxd-guest-fw)"
    done
    if [ -n "$RELAY_USER" ]; then
        nft list chain inet boxd_guest relay_only >/dev/null 2>&1 \
            || die "chain inet boxd_guest relay_only is not loaded: the relay would run unfiltered (boxd-guest-fw needs --relay-user $RELAY_USER)"
        echo "boxd-guest-fw: ${#TABLES[@]} table(s) and the relay's chain loaded"
        exit 0
    fi
    echo "boxd-guest-fw: ${#TABLES[@]} table(s) loaded"
    exit 0
fi

if [ "$MODE" = metrics ]; then
    # Counters reset when the ruleset is re-applied; Prometheus counters
    # handle that as a reset.
    tmp=$(mktemp "$METRICS_FILE.XXXXXX")
    BOXD_GUEST_FW_ROUTED=$ROUTED python3 - > "$tmp" <<'PY'
import json, os, subprocess, time

def esc(v):
    return v.replace("\\", "\\\\").replace('"', '\\"')

loaded, frames, drops = {}, None, []
families = ("inet",) if os.environ.get("BOXD_GUEST_FW_ROUTED") == "1" else ("bridge", "inet")
for fam in families:
    r = subprocess.run(["nft", "-j", "list", "table", fam, "boxd_guest"],
                       capture_output=True, text=True)
    loaded[fam] = int(r.returncode == 0)
    if r.returncode != 0:
        continue
    for item in json.loads(r.stdout)["nftables"]:
        rule = item.get("rule")
        if not rule:
            continue
        exprs = [e for e in rule.get("expr", []) if isinstance(e, dict)]
        packets = next((e["counter"]["packets"] for e in exprs if "counter" in e), None)
        if packets is None:
            continue
        comment = rule.get("comment", "")
        if any("drop" in e for e in exprs):
            drops.append((fam, rule["chain"], comment, packets))
        elif comment == "guest frames":
            frames = packets

print("# HELP boxd_guest_fw_table_loaded 1 if this guest firewall table is loaded (scripts/boxd-guest-fw.sh).")
print("# TYPE boxd_guest_fw_table_loaded gauge")
for fam, ok in loaded.items():
    print(f'boxd_guest_fw_table_loaded{{table="{fam} boxd_guest"}} {ok}')
print("# HELP boxd_guest_fw_loaded 1 if every guest firewall table is loaded.")
print("# TYPE boxd_guest_fw_loaded gauge")
print(f"boxd_guest_fw_loaded {int(all(loaded.values()))}")
if frames is not None:
    print("# HELP boxd_guest_fw_guest_frames_total Frames from guest taps since the ruleset was applied.")
    print("# TYPE boxd_guest_fw_guest_frames_total counter")
    print(f"boxd_guest_fw_guest_frames_total {frames}")
if drops:
    print("# HELP boxd_guest_fw_dropped_packets_total Guest packets dropped, per rule, since the ruleset was applied.")
    print("# TYPE boxd_guest_fw_dropped_packets_total counter")
    for fam, chain, comment, packets in drops:
        print(f'boxd_guest_fw_dropped_packets_total{{table="{fam} boxd_guest",chain="{esc(chain)}",rule="{esc(comment)}"}} {packets}')
print("# HELP boxd_guest_fw_check_timestamp_seconds When this check ran.")
print("# TYPE boxd_guest_fw_check_timestamp_seconds gauge")
print(f"boxd_guest_fw_check_timestamp_seconds {int(time.time())}")
PY
    chmod 0644 "$tmp"
    mv -f "$tmp" "$METRICS_FILE"
    exit 0
fi

if [ "$MODE" = remove ]; then
    # Table by table: a kernel without the bridge family can't even name it.
    for t in "bridge boxd_guest" "inet boxd_guest"; do
        # shellcheck disable=SC2086
        if nft list table $t >/dev/null 2>&1; then nft delete table $t; fi
    done
    echo "boxd-guest-fw: removed"
    exit 0
fi

[ -n "$SUBNET" ] || die "--subnet is required"
[ -n "$GATEWAY" ] || die "--gateway is required"
check_subnet "$SUBNET" --subnet
P=$(prefix16 "$SUBNET")
INFRA24="$P.0.0/24"
is_ipv4 "$GATEWAY" && [[ "$GATEWAY" == "$P.0."* ]] \
    || die "--gateway must be an address in $INFRA24, got '$GATEWAY'"

for s in ${PEERS[@]+"${PEERS[@]}"}; do
    check_subnet "$s" --peer-subnet
    [ "$s" != "$SUBNET" ] || die "--peer-subnet $s is this worker's own subnet"
done
if [ -n "$ROUTED_POOL" ]; then
    [ "$ROUTED" = 0 ] || die "--routed-pool is for bridge workers (a routed worker's pool is --subnet)"
    check_subnet "$ROUTED_POOL" --routed-pool
    [ "$ROUTED_POOL" != "$SUBNET" ] || die "--routed-pool $ROUTED_POOL is this worker's own subnet"
    for s in ${PEERS[@]+"${PEERS[@]}"}; do
        [ "$s" != "$ROUTED_POOL" ] || die "--routed-pool $ROUTED_POOL is also a --peer-subnet"
    done
fi
for a in ${PROXIES[@]+"${PROXIES[@]}"}; do
    is_ipv4 "$a" && [[ "$a" == "$P.0."* ]] \
        || die "--proxy must be an on-link address in $INFRA24, got '$a'"
done
for g in ${LEGACY_GWS[@]+"${LEGACY_GWS[@]}"}; do
    is_ipv4 "$g" && [[ "$g" =~ ^10\.[0-9]+\.0\.1$ ]] \
        || die "--legacy-gateway must be an old gateway 10.N.0.1, got '$g'"
    [ "$ROUTED" = 1 ] || die "--legacy-gateway is for --routed (bridge workers have boxd-legacy-gw-compat)"
done
for p in ${HOST_PORTS[@]+"${HOST_PORTS[@]}"}; do
    [[ "$p" =~ ^[0-9]{1,5}$ ]] && (( p >= 1 && p <= 65535 )) \
        || die "--host-tcp-port must be a port number, got '$p'"
done
[ ${#PRIVATE_SERVICES[@]} -eq 0 ] || [ -n "$RELAY_USER" ] || die "--private-service needs --relay-user"
for x in ${PRIVATE_SERVICES[@]+"${PRIVATE_SERVICES[@]}"}; do
    bad="--private-service must be CIDR:PORT (e.g. 10.70.1.0/24:443, 10.70.1.50/32:5432), got '$x'"
    [[ "$x" =~ ^([0-9.]+)/([0-9]{1,2}):([0-9]{1,5})$ ]] || die "$bad"
    a=${BASH_REMATCH[1]} l=${BASH_REMATCH[2]} p=${BASH_REMATCH[3]}   # is_ipv4 overwrites BASH_REMATCH
    is_ipv4 "$a" && (( l <= 32 && p >= 1 && p <= 65535 )) || die "$bad"
done

join() { local IFS=, s; s="$*"; echo "${s//,/, }"; }

# What a guest may reach on its own bridge segment: other guests and the
# proxies' device ingress. Used by the bridge forward chain and, for packets
# leaving onto a bridge, by the inet one.
onlink_rules() {
    # Broadcast and multicast flood out the vxlan port to every proxy and peer
    # worker, which answer on anything bound to 0.0.0.0. Unicast only, by frame
    # and by address (a broadcast address in a unicast frame is still
    # delivered as broadcast by the receiver's IP stack).
    echo "		meta pkttype { broadcast, multicast } counter drop comment \"broadcast/multicast would flood to proxies and peers\""
    echo "		ip daddr { $P.255.255, 255.255.255.255, 224.0.0.0/4 } counter drop comment \"broadcast/multicast addresses\""
    echo "		ip daddr $SUBNET ip daddr != $INFRA24 accept comment \"guests on this bridge; their taps apply tenant rules\""
    if [ ${#PROXIES[@]} -gt 0 ]; then
        echo "		ip daddr { $(join "${PROXIES[@]}") } tcp dport $DEVICE_INGRESS_PORT accept comment \"device ingress\""
    fi
    echo "		counter drop comment \"on-link infrastructure: proxies, peer workers, anything else\""
}

# The relay for private services, in the inet table of both renderers: what boxes may reach of
# it, and what its own user may reach. The match on the user is positive and jumps: a packet the
# kernel makes with no socket (a reset, an ICMP error) has no user, matches nothing and takes the
# chain's accept.
relay_input_rule() {
    echo "		ip daddr $GATEWAY tcp dport $RELAY_PORT ct status dnat accept comment \"private services: steered connections only\""
}

relay_chains() {
    [ -n "$RELAY_USER" ] || return 0
    cat <<EOF

	chain relay_out {
		type filter hook output priority filter; policy accept;
		meta skuid "$RELAY_USER" jump relay_only
	}

	chain relay_only {
		ct state established,related accept
		oifname "lo" accept comment "the host's stub resolver, the relay's own metrics"
		meta l4proto { tcp, udp } th dport 53 accept comment "the host's resolver"
EOF
    local x
    for x in ${PRIVATE_SERVICES[@]+"${PRIVATE_SERVICES[@]}"}; do
        echo "		ip daddr ${x%:*} tcp dport ${x##*:} accept comment \"private service\""
    done
    cat <<EOF
		counter drop comment "relay: outside the declared services"
	}
EOF
}

render() {
    local peer_subnets=() peer_gateways=() s
    for s in ${PEERS[@]+"${PEERS[@]}"}; do
        peer_subnets+=("$s")
        peer_gateways+=("$(prefix16 "$s").0.1")
    done
    # The routed workers' machines; their gateway is no bridge worker's (never dialled
    # from here), and the 10.x.0.y drop below keeps the pool's hosts out.
    [ -z "$ROUTED_POOL" ] || peer_subnets+=("$ROUTED_POOL")

    remove_batch
    cat <<EOF

table bridge boxd_guest {
	# The ct rules below turn on bridge conntrack, which would confirm a br1
	# guest's flow at br1 with no NAT binding; at br0 the IP layer then finds
	# that confirmed entry and MASQUERADE and the egress REDIRECT never bind
	# (they only bind on a flow's first, unconfirmed packet). boxd-br1 has no
	# address and is re-judged on br0, so it needs no conntrack of its own.
	chain raw {
		type filter hook prerouting priority -300; policy accept;
		meta ibrname "$BR1" notrack comment "keep br1 flows NATable at br0"
	}

	chain prerouting {
		type filter hook prerouting priority filter; policy accept;
		iifname != "$TAP_GLOB" accept
		ether type != { ip, arp } counter drop comment "IPv6, VLAN-tagged and other non-IPv4 frames"
		counter meta mark set meta mark or $MARK comment "guest frames"
	}

	chain forward {
		type filter hook forward priority filter; policy accept;
		meta mark and $MARK == 0 accept
		ether type arp accept
		ct state established,related accept
		oifname "$BR1_VETH" accept comment "boxd-br1 -> boxd-br0: filtered again there"
EOF
    onlink_rules
    cat <<EOF
	}
}

table inet boxd_guest {
	chain input {
		type filter hook input priority filter; policy accept;
		meta mark and $MARK == 0 accept
		ct state established,related accept
		ip daddr $GATEWAY tcp dport { $GW_TCP_PORTS } accept
		ip daddr $GATEWAY udp dport { $GW_UDP_PORTS } accept
		ip daddr $GATEWAY icmp type echo-request accept
EOF
    relay_input_rule
    if [ ${#HOST_PORTS[@]} -gt 0 ]; then
        echo "		tcp dport { $(join "${HOST_PORTS[@]}") } accept comment \"--host-tcp-port\""
    fi
    cat <<EOF
		counter drop comment "this host's other ports and addresses"
	}

	chain onlink {
EOF
    onlink_rules
    cat <<EOF
	}

	chain forward {
		type filter hook forward priority filter; policy accept;
		meta mark and $MARK == 0 accept
		ct state established,related accept
		iifname "$BR1" accept comment "bridged on $BR1 (br_netfilter): no address, never routed; the bridge table judges it"
		oifname { "$BR0", "$BR1" } jump onlink comment "bridged (br_netfilter) or hairpinned onto the bridge"
EOF
    if [ ${#peer_gateways[@]} -gt 0 ]; then
        cat <<EOF
		ip daddr { $(join "${peer_gateways[@]}") } tcp dport { $GW_TCP_PORTS } accept comment "peer gateways"
		ip daddr { $(join "${peer_gateways[@]}") } udp dport { $GW_UDP_PORTS } accept comment "peer gateways"
EOF
    fi
    cat <<EOF
		ip daddr and 255.0.255.0 == 10.0.0.0 counter drop comment "10.x.0.y infrastructure (the tap eBPF's INFRA_MASK)"
EOF
    if [ ${#peer_subnets[@]} -gt 0 ]; then
        echo "		ip daddr { $(join "${peer_subnets[@]}") } accept comment \"guests on peer workers\""
    fi
    cat <<EOF
		ip daddr { $NONPUBLIC } counter drop comment "control network and other non-internet ranges"
	}
EOF
    relay_chains
    echo "}"
}

# A routed worker's ruleset (--routed): no bridge, so the inet table alone. The
# eBPF program on each tap has already dropped spoofed, non-IPv4 and
# machine-to-machine frames by the time anything reaches these hooks.
render_routed() {
    local old_gws=() s
    for s in ${LEGACY_GWS[@]+"${LEGACY_GWS[@]}"}; do old_gws+=("$s"); done
    for s in ${PEERS[@]+"${PEERS[@]}"}; do old_gws+=("$(prefix16 "$s").0.1"); done

    printf 'table inet boxd_guest {}\ndelete table inet boxd_guest\n'
    cat <<EOF

table inet boxd_guest {
EOF
    if [ ${#old_gws[@]} -gt 0 ]; then
        cat <<EOF
	chain old_gateways {
		type nat hook prerouting priority dstnat; policy accept;
		iifname "$TAP_GLOB" ip daddr { $(join "${old_gws[@]}") } dnat ip to $GATEWAY comment "old gateways"
	}

EOF
    fi
    cat <<EOF
	chain prerouting {
		type filter hook prerouting priority filter; policy accept;
		iifname != "$TAP_GLOB" accept
		counter comment "guest frames"
		meta nfproto ipv6 counter drop comment "IPv6 (the tap program drops it first)"
	}

	chain input {
		type filter hook input priority filter; policy accept;
		iifname != "$TAP_GLOB" accept
		ct state established,related accept
		ip daddr $GATEWAY tcp dport { $GW_TCP_PORTS } accept
		ip daddr $GATEWAY udp dport { $GW_UDP_PORTS } accept
		ip daddr $GATEWAY icmp type echo-request accept
EOF
    relay_input_rule
    cat <<EOF
		counter drop comment "this host's other ports and addresses"
	}

	chain forward {
		type filter hook forward priority filter; policy accept;
		iifname != "$TAP_GLOB" accept
		ct state established,related accept
EOF
    if [ ${#PROXIES[@]} -gt 0 ]; then
        echo "		ip daddr { $(join "${PROXIES[@]}") } tcp dport $DEVICE_INGRESS_PORT accept comment \"device ingress\""
    fi
    cat <<EOF
		ip daddr $SUBNET counter drop comment "the pool's hosts and free addresses (machines never reach the stack)"
EOF
    if [ ${#PEERS[@]} -gt 0 ]; then
        echo "		ip daddr { $(join "${PEERS[@]}") } accept comment \"machines on bridge workers\""
    fi
    cat <<EOF
		ip daddr { $NONPUBLIC } counter drop comment "control network and other non-internet ranges"
	}
EOF
    relay_chains
    echo "}"
}

bridge_supported() {
    printf 'table bridge boxd_guest_probe {\n\tchain c {\n\t\ttype filter hook forward priority filter;\n\t\tct state established accept\n\t}\n}\n' \
        | nft -c -f - >/dev/null 2>&1
}

if [ "$ROUTED" = 1 ]; then
    case "$MODE" in
        print) render_routed ;;
        check) render_routed | nft -c -f -; echo "boxd-guest-fw: routed ruleset OK" ;;
        apply)
            render_routed | nft -f -
            echo "boxd-guest-fw: applied, routed (pool $SUBNET, ${#PEERS[@]} bridge worker subnet(s), ${#PROXIES[@]} proxy address(es))"
            ;;
    esac
    exit 0
fi

case "$MODE" in
    print)
        render
        ;;
    check|apply)
        if ! bridge_supported; then
            msg="kernel $(uname -r) lacks nf_tables bridge or bridge conntrack (CONFIG_NF_TABLES_BRIDGE, CONFIG_NF_CONNTRACK_BRIDGE)"
            if [ "$BEST_EFFORT" = 1 ]; then
                echo "boxd-guest-fw: WARNING: $msg; guests are NOT firewalled" >&2
                exit 0
            fi
            die "$msg"
        fi
        if [ "$MODE" = check ]; then
            render | nft -c -f -
            echo "boxd-guest-fw: ruleset OK"
        else
            render | nft -f -
            echo "boxd-guest-fw: applied (subnet $SUBNET, ${#PEERS[@]} peer(s), ${#PROXIES[@]} proxy address(es))"
        fi
        ;;
esac

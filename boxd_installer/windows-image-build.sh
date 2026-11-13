#!/usr/bin/env bash
# ./boxd-install windows-image build: the cluster's Windows image, built on this host and pushed
# to the registry [windows] image names (win-img/README.md, "Building the image"). boxd-install
# runs `install.py windows-image kit` in the tools image first (the release bundle's Windows image
# kit into cache/windows-image/<release>/, the registry login beside it) and hands its output here
# in BOXD_KIT_INFO; the build itself needs this host's /dev/kvm and Docker, which the tools image
# doesn't have. Then, on this host:
#   1. boxd's inputs into the kit's dl/ (fetch-tools.sh with BOXD_ONLY=1: the virtio-win drivers,
#      the ConPTY), and your layer's files into win-img/files/ (win-img/fetch.sh);
#   2. the install under QEMU (the kit's build-image.sh): Windows from your ISO, boxd's setup,
#      your win-img/install.ps1 and on-clone.d/ hooks, then sysprep. About 15 minutes;
#   3. the image packaged and pushed (the kit's package-image.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

usage() {
    cat >&2 <<'USAGE'
usage: ./boxd-install windows-image build --iso FILE [options]

  --iso FILE               your Windows Server 2025 ISO (win-img/README.md, "The Windows ISO")
  --product-key-file FILE  the product key Windows Setup needs for volume-licensing media: the
                           edition's KMS client setup key (none for the evaluation ISO)
  --index N                the ISO's edition, its image index (default 4: Datacenter with
                           Desktop Experience on the evaluation and on volume media)
  --size SIZE              the image's disk (default 32G; every machine's disk grows past it)
  --name NAME              the build's name on this host (default win-<UTC time>)
  --keep                   keep the built disk under cache/windows-image/ after the push

The image goes to [windows] image in cluster.toml, with the providers config's
[windows_registry] login (else this host's own `docker login`). Needs /dev/kvm, Docker and about
100 GB free here.
USAGE
    exit "${1:-2}"
}
die() { echo "boxd-install windows-image: $*" >&2; exit 1; }

ISO= KEYFILE= INDEX=4 SIZE=32G NAME= KEEP=
while [ $# -gt 0 ]; do
    case "$1" in
        --iso) ISO=${2:?--iso needs a file}; shift 2 ;;
        --product-key-file) KEYFILE=${2:?--product-key-file needs a file}; shift 2 ;;
        --index) INDEX=${2:?--index needs a number}; shift 2 ;;
        --size) SIZE=${2:?--size needs a size}; shift 2 ;;
        --name) NAME=${2:?--name needs a name}; shift 2 ;;
        --keep) KEEP=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option $1" >&2; usage ;;
    esac
done
[ -n "$ISO" ] || usage
[ -f "$ISO" ] || die "no ISO at $ISO"
[ -z "$KEYFILE" ] || [ -f "$KEYFILE" ] || die "no product key file at $KEYFILE"
case "$INDEX" in [1-9]) ;; *) die "--index is the ISO's image index, 1-9" ;; esac
case "$SIZE" in [1-9]*G) ;; *) die "--size is a size in G, e.g. 32G" ;; esac
NAME=${NAME:-win-$(date -u +%Y%m%d-%H%M%S)}
case "$NAME" in *[!A-Za-z0-9._-]*) die "--name takes letters, digits, '.', '_' and '-'" ;; esac
# Docker hands /dev/kvm to the build's container, so it only has to exist here, and Docker be usable.
[ -c /dev/kvm ] ||
    die "this host has no /dev/kvm: build on a Linux host with KVM (on a cloud VM, nested virtualization on)"
docker info >/dev/null 2>&1 || die "Docker isn't usable here (as this user: the docker group, or root)"

field() { printf '%s\n' "${BOXD_KIT_INFO:-}" | sed -n "s/^$1=//p" | tail -1; }
KIT_REL=$(field kit) IMAGE=$(field image) RELEASE=$(field release) DOCKER_REL=$(field docker_config)
[ -n "$KIT_REL" ] && [ -n "$IMAGE" ] || die "no kit from install.py windows-image kit (run it through ./boxd-install)"
KIT=$PWD/$KIT_REL
DOCKER_DIR=${DOCKER_REL:+$PWD/$DOCKER_REL}

# The registry login and the image's Administrator password go whatever happens; the disks too,
# unless --keep.
cleanup() {
    rm -rf "${DOCKER_DIR:-/nonexistent}"
    rm -f "$KIT/admin-password" "$KIT/product-key"
    if [ -z "$KEEP" ]; then
        rm -f "$KIT/$NAME.raw" "$KIT/$NAME.pristine.raw"
    fi
}
trap cleanup EXIT

echo "==> building $IMAGE from $ISO (release ${RELEASE:0:12}), as $NAME"
# Your ISO where the kit looks for it: a hard link on the same file system, else a copy.
mkdir -p "$KIT/dl"
rm -f "$KIT/dl/win2025-eval.iso"
ln "$ISO" "$KIT/dl/win2025-eval.iso" 2>/dev/null || cp --reflink=auto "$ISO" "$KIT/dl/win2025-eval.iso"
# The image's own Administrator password, for the build only: every machine made from the image
# gets a random one at its first boot (boxd-ready.exe), and its owner sets theirs through boxd.
( umask 077
  printf '%sAa1-\n' "$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)" > "$KIT/admin-password"
  if [ -n "$KEYFILE" ]; then cp "$KEYFILE" "$KIT/product-key"; else rm -f "$KIT/product-key"; fi )

echo "==> boxd's inputs (the drivers, the ConPTY) and your layer's files"
BOXD_ONLY=1 L="$KIT" sh "$KIT/fetch-tools.sh"
sh win-img/fetch.sh

echo "==> the install (about 15 minutes)"
L="$KIT" CUSTOM="$PWD/win-img" INDEX="$INDEX" sh "$KIT/build-image.sh" "$NAME" "$SIZE"

echo "==> packaging and pushing $IMAGE"
PUSH_CONFIG="${DOCKER_DIR:-}" L="$KIT" sh "$KIT/package-image.sh" "$NAME" "$IMAGE"
echo "==> pushed $IMAGE. Run ./boxd-install apply to make it the cluster's Windows image."

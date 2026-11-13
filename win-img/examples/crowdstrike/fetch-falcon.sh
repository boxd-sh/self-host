#!/bin/sh
# fetch-falcon.sh: the CrowdStrike Falcon sensor for Windows and your CID, from Falcon's API, into
# win-img/files/ (FalconSensor_Windows.exe, falcon-cid.txt), for install-falcon.ps1 to install in
# the image. Run it from win-img/fetch.sh (README.md, "Example: CrowdStrike Falcon"), so
# `./boxd-install windows-image build` runs it on the build host.
#
# A Falcon API client with the scope Sensor download: Read (Falcon console: Support and resources →
# API clients and keys), in the build host's environment; nothing is stored:
#   FALCON_CLIENT_ID, FALCON_CLIENT_SECRET   the API client
#   FALCON_CLOUD                             us-1 (default), us-2, eu-1 or us-gov-1
#   FALCON_SENSOR_VERSION                    optional: this version (e.g. 7.20.19011), else the
#                                            newest. Match your hosts' sensor update policy: it
#                                            moves every machine's sensor to its own version anyway
# The installer's SHA-256 is checked against the one Falcon lists for it.
set -eu
OUT=${OUT:-$(dirname "$0")/../../files}
: "${FALCON_CLIENT_ID:?set FALCON_CLIENT_ID (a Falcon API client with Sensor download: Read)}"
: "${FALCON_CLIENT_SECRET:?set FALCON_CLIENT_SECRET}"
case "${FALCON_CLOUD:-us-1}" in
  us-1) API=https://api.crowdstrike.com ;;
  us-2) API=https://api.us-2.crowdstrike.com ;;
  eu-1) API=https://api.eu-1.crowdstrike.com ;;
  us-gov-1) API=https://api.laggar.gcw.crowdstrike.com ;;
  *) echo "FALCON_CLOUD is us-1, us-2, eu-1 or us-gov-1, not $FALCON_CLOUD" >&2; exit 1 ;;
esac
mkdir -p "$OUT"
cd "$OUT"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# The client's secret goes in on stdin, never on a command line.
printf 'client_id=%s&client_secret=%s' "$FALCON_CLIENT_ID" "$FALCON_CLIENT_SECRET" |
  curl -fsS --retry 3 -X POST "$API/oauth2/token" -H 'Content-Type: application/x-www-form-urlencoded' \
    --data-binary @- -o "$tmp/token.json" ||
  { echo "Falcon refused the API client (FALCON_CLIENT_ID, FALCON_CLIENT_SECRET, FALCON_CLOUD)" >&2; exit 1; }
json() { python3 -I -c "import json,sys; d=json.load(open(sys.argv[1])); print($2)" "$1"; }
token=$(json "$tmp/token.json" 'd["access_token"]')
# The header with the token from a file, never a command line.
printf 'Authorization: Bearer %s\n' "$token" > "$tmp/auth"
get() { curl -fsS --retry 3 -H @"$tmp/auth" "$@"; }

get -o "$tmp/ccid.json" "$API/sensors/queries/installers/ccid/v1" ||
  { echo "the API client can't read your CID: it needs the scope Sensor download: Read" >&2; exit 1; }
json "$tmp/ccid.json" 'd["resources"][0]' > falcon-cid.txt

if [ -n "${FALCON_SENSOR_VERSION:-}" ]; then
  filter="platform:\"windows\"+version:\"$FALCON_SENSOR_VERSION\""
else
  filter='platform:"windows"'
fi
get -G -o "$tmp/installers.json" "$API/sensors/combined/installers/v2" \
  --data-urlencode "filter=$filter" --data-urlencode 'sort=version|desc' --data-urlencode 'limit=1'
sha=$(json "$tmp/installers.json" 'd["resources"][0]["sha256"] if d["resources"] else ""')
version=$(json "$tmp/installers.json" 'd["resources"][0]["version"] if d["resources"] else ""')
[ -n "$sha" ] || { echo "Falcon lists no Windows sensor${FALCON_SENSOR_VERSION:+ $FALCON_SENSOR_VERSION}" >&2; exit 1; }

if [ -f FalconSensor_Windows.exe ] && echo "$sha  FalconSensor_Windows.exe" | sha256sum -c --status 2>/dev/null; then
  echo "have FalconSensor_Windows.exe $version"
else
  echo "fetching FalconSensor_Windows.exe $version"
  get -o FalconSensor_Windows.exe.part "$API/sensors/entities/download-installer/v2?id=$sha"
  echo "$sha  FalconSensor_Windows.exe.part" | sha256sum -c --status ||
    { rm -f FalconSensor_Windows.exe.part; echo "the sensor's SHA-256 is not the one Falcon lists" >&2; exit 1; }
  mv FalconSensor_Windows.exe.part FalconSensor_Windows.exe
fi
echo "Falcon sensor $version and your CID in $(pwd)"

#!/bin/sh
# fetch.sh: the software your Windows image carries, downloaded on the build host and checked
# against a pinned SHA-256, into win-img/files/ (git-ignored). The image build has no network: it
# puts files/ on the answer ISO as custom\files, and install.ps1 installs from there.
#
# Yours to edit. Add a file: a `fetch NAME URL SHA256` line, then the install in install.ps1.
# Drop one: remove both. A download whose SHA-256 isn't the pinned one stops the fetch.
#
# boxd's default set, for coding agents on Windows machines:
#   git-installer.exe     Git for Windows, the 64-bit offline installer (Claude Code's Bash tool
#                         runs in its Git Bash)
#   claude.exe            Claude Code's native Windows x64 binary (the release bucket that
#                         https://claude.ai/install.ps1 reads; the SHA-256 is the release's
#                         manifest.json platforms.win32-x64.checksum)
#   codex-package.tar.gz  Codex's complete Windows x64 package (github.com/openai/codex releases;
#                         its app-server daemon refuses a bare codex.exe)
#   bun.zip               Bun for Windows x64, the baseline build (no AVX2 needed, so the image runs
#                         on any worker CPU)
#   gh.zip                the GitHub CLI for Windows amd64 (github.com/cli/cli releases)
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${OUT:-$HERE/files}

GIT_URL=https://github.com/git-for-windows/git/releases/download/v2.56.1.windows.1/Git-2.56.1-64-bit.exe
GIT_SHA256=c330c72e10b408cc39994bfefdb63542b0d93d21699325c3e0bd874af8a5a559

CLAUDE_URL=https://downloads.claude.ai/claude-code-releases/2.1.311/win32-x64/claude.exe
CLAUDE_SHA256=966f4b2ce962298f5dee6a942e8c47c8f97392e69e728e622319a98b259c0680

CODEX_URL=https://github.com/openai/codex/releases/download/rust-v0.167.2/codex-package-x86_64-pc-windows-msvc.tar.gz
CODEX_SHA256=20b0f94105f40b617bf209b1679b8674ab6d3d8f303e767ae43cf1860a8705da

BUN_URL=https://github.com/oven-sh/bun/releases/download/bun-v1.4.4/bun-windows-x64-baseline.zip
BUN_SHA256=0f20ce6674d4f2dbcc8c5d59fdf99f7ba45f8c3b367e0f5781cbfa30a1c78d99

GH_URL=https://github.com/cli/cli/releases/download/v2.103.3/gh_2.103.3_windows_amd64.zip
GH_SHA256=98d0cfa794721665d7973dd728c78aff7bd836d58f10f6d7a03568eccb0a22e0

mkdir -p "$OUT"
cd "$OUT"

# fetch NAME URL SHA256: a file already there with the right SHA-256 is kept.
fetch() {
  if [ -f "$1" ] && echo "$3  $1" | sha256sum -c --status 2>/dev/null; then
    echo "have $1"
    return
  fi
  echo "fetching $1"
  curl -fsSL --retry 3 -o "$1.part" "$2"
  if ! echo "$3  $1.part" | sha256sum -c --status; then
    rm -f "$1.part"
    echo "$1: the download's SHA-256 is not the pinned one ($2)" >&2
    exit 1
  fi
  mv "$1.part" "$1"
}

# CrowdStrike Falcon (README.md, "Example: CrowdStrike Falcon"): the sensor and your CID, with a
# Falcon API client in FALCON_CLIENT_ID, FALCON_CLIENT_SECRET and FALCON_CLOUD:
#   OUT="$PWD" sh "$HERE/examples/crowdstrike/fetch-falcon.sh"

fetch git-installer.exe "$GIT_URL" "$GIT_SHA256"
fetch claude.exe "$CLAUDE_URL" "$CLAUDE_SHA256"
fetch codex-package.tar.gz "$CODEX_URL" "$CODEX_SHA256"
fetch bun.zip "$BUN_URL" "$BUN_SHA256"
fetch gh.zip "$GH_URL" "$GH_SHA256"
echo "win-img: files in $OUT"

#!/usr/bin/env python3
"""boxd-assets-unpack.py ROOT SHA: unpack ROOT/SHA.zip (the assets bundle)
into ROOT/SHA/ and check it against ROOT/SHA.hashes.json (rel -> sha256,
from the manifest the installer verified on the operator's machine). Keeps
the zip's file modes, which zipfile drops. Writes ROOT/SHA/.verified last,
then removes everything else under ROOT: the zip, the hashes, and older
versions. Any failure exits non-zero and leaves no .verified."""
import hashlib
import json
import os
import shutil
import sys
import zipfile

root, sha = sys.argv[1], sys.argv[2]
tmp, dest = os.path.join(root, sha + ".extracting"), os.path.join(root, sha)
shutil.rmtree(tmp, ignore_errors=True)
with zipfile.ZipFile(os.path.join(root, sha + ".zip")) as zf:
    for info in zf.infolist():
        name = info.filename
        if name.startswith("/") or ".." in name.split("/"):
            sys.exit(f"unsafe path in the assets zip: {name}")
        zf.extract(info, tmp)
        mode = (info.external_attr >> 16) & 0o7777
        if mode and not name.endswith("/"):
            os.chmod(os.path.join(tmp, name), mode)
inner = os.path.join(tmp, "boxd-assets")
with open(os.path.join(root, sha + ".hashes.json")) as fh:
    want = json.load(fh)
for rel, digest in want.items():
    h = hashlib.sha256()
    with open(os.path.join(inner, rel), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != digest:
        sys.exit(f"assets zip: checksum mismatch on {rel}")
shutil.rmtree(dest, ignore_errors=True)
os.rename(inner, dest)
open(os.path.join(dest, ".verified"), "w").close()
for name in os.listdir(root):
    if name != sha:
        path = os.path.join(root, name)
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)

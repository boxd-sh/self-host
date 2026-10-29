# boxd release public keys

One PEM per key ID: `<key_id>.pem`. The key ID is the version of boxd's
Ed25519 release signing key, and the `key_id` in every `.sig` file. The
installer checks `release.json` and the channel pointers with these keys
(`boxd_installer/assets.py`, `openssl pkeyutl -verify -rawin`). They are
public keys: not secrets.

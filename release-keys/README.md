# boxd release public keys

One PEM per key ID: `<key_id>.pem`. The key ID is the Cloud KMS version of
boxd's `release` signing key (Cloud KMS, `boxd-prod-491613`, `europe`, key ring `release`, key `signing`, Ed25519), and the `key_id` in every `.sig` file. The
installer checks `release.json` and the channel pointers with these keys
(`boxd_installer/assets.py`, `openssl pkeyutl -verify -rawin`). They are
public keys: not secrets. The boxd repo holds the same files in
`release/release-keys/`.

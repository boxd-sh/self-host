# Contributing

Issues and pull requests are welcome: bugs, questions, missing features,
unclear docs.

## Branches

- `dev` is where work happens. Open pull requests against `dev`.
- `main` holds the latest release, and is what the install guide clones. A
  release moves `main` forward to a commit of `dev`, tags it
  (`vYYYY.MM.DD`) and publishes a GitHub Release with its notes.
- `release/v1` is the line for clusters on boxd v1. A fix that v1 clusters
  need lands on `dev` first, then in a pull request against `release/v1`.

## Running the tests

The tests need Python 3.12 or newer and nothing else; the ones that need
terraform or ansible skip without them.

```sh
python3 -m unittest discover -s tests
```

To run them with every tool, in the image the installer runs in:

```sh
./boxd-install shell -c 'python3 -m unittest discover -s tests'
```

## The tools image

`docker/` builds the image `./boxd-install` runs the installer in. To change a
pinned tool, edit `docker/Dockerfile` and bump `docker/VERSION` (date form,
`2026.10.30`, then `.1`, `.2` for more the same day). After the merge to `dev`, CI publishes
`ghcr.io/boxd-sh/self-host-tools:<VERSION>`. It never overwrites a published
version.

## Security

Do not report security problems in a public issue. Follow
[SECURITY.md](SECURITY.md).

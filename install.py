#!/usr/bin/env python3
"""boxd self-host installer for GCP and AWS. Stdlib only (Python >= 3.11).

    ./install.py validate                 check cluster.toml
    ./install.py render                   regenerate out/ from cached tf outputs
    ./install.py render --fake-tf FILE    render from a fake tf-outputs JSON
    ./install.py apply                    full pipeline: assets -> terraform ->
                                          render -> ansible -> post-install
    ./install.py destroy                  terraform destroy (state/ untouched)
"""
import sys

if sys.version_info < (3, 11):
    sys.exit("install.py needs Python >= 3.11 (tomllib)")

from boxd_installer.main import main

if __name__ == "__main__":
    sys.exit(main())

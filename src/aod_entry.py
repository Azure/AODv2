"""Entry point for the AODv2 daemon.
Runs a dependency preflight before importing any application module.
Kept intentionally tiny; do not add application logic here.
"""

from utils.preflight import verify_runtime_deps

verify_runtime_deps()

# Only after the preflight passes do we pull in the application modules.
from Controller import main  # noqa: E402

if __name__ == "__main__":
    main()

"""``python -m aqdt`` — alias of the ``aqdt`` console script."""

# @spec PIPE-CLI-001
from aqdt.pipeline.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

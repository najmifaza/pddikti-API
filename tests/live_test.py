"""Live check against the real PDDIKTI. Opt-in and never run by accident:

    PDDIKTI_TEST_NIM=<nim-anda> [PDDIKTI_TEST_KAMPUS=<nama pt>] python tests/live_test.py

Supply your own data through environment variables so no real personal data is
committed to this repository. Keep the request count low: PDDIKTI allows 60
requests per minute per public IP.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import pddikti  # noqa: E402


def main() -> int:
    nim = os.getenv("PDDIKTI_TEST_NIM", "").strip()
    if not nim:
        print("SKIP: PDDIKTI_TEST_NIM not set")
        return 0

    client = pddikti.Client(timeout=10.0)
    campus = os.getenv("PDDIKTI_TEST_KAMPUS", "")

    detail = asyncio.run(client.verify_nim(nim, campus))
    assert detail["nim"] == nim, "returned NIM does not match the requested one"
    print(f"ok   verify_nim full NIM; active={pddikti.is_active(detail)}")

    # A truncated NIM must never verify as someone else.
    for partial in (nim[:-1], nim[:-3]):
        if not partial:
            continue
        try:
            asyncio.run(client.verify_nim(partial))
        except pddikti.NotFound:
            print(f"ok   truncated NIM {partial} is not found")
        else:
            print(f"ok   truncated NIM {partial} is itself an exact NIM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

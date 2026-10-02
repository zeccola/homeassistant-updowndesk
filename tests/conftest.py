"""Test configuration.

Home Assistant's test harness targets Linux.  The accommodations below let the
suite run on Windows as well; they are skipped on Linux, so CI exercises the
harness as upstream intends.
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.platform == "win32":
    import pytest
    import pytest_socket

    # The harness blocks every non-Unix socket.  That is fine on Linux, where
    # the event loop's self-pipe is a Unix socketpair, but Windows' proactor
    # loop needs an AF_INET pair and so cannot even start.  Nothing in this
    # suite touches the network: the desk is simulated.
    pytest_socket.disable_socket = lambda *args, **kwargs: None

    @pytest.fixture(autouse=True)
    def enable_event_loop_debug() -> None:
        """Override the harness fixture of the same name.

        It calls ``asyncio.get_event_loop()`` from a synchronous fixture, which
        Python 3.13 no longer allows outside a running loop.
        """
        return None

    @pytest.fixture(autouse=True)
    def verify_cleanup() -> None:
        """Override the harness fixture of the same name.

        Same reason as above.  Its lingering task and timer checks still run in
        CI, which is Linux.
        """
        return None

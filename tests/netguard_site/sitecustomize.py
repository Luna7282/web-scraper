"""Installs the test network guard in every child Python process started
during the test suite (tests/netguard.py puts this directory on PYTHONPATH).
Does nothing unless SCRAPER_NETGUARD=1 and the running test isn't allowed."""

import os

if os.environ.get("SCRAPER_NETGUARD") == "1" and os.environ.get("SCRAPER_NETGUARD_ALLOW") != "1":
    import importlib.util

    _path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "netguard.py")
    _spec = importlib.util.spec_from_file_location("_scraper_netguard", _path)
    _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    _module.install()

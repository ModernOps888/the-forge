#!/usr/bin/env python3
"""Entry point alias for The Forge v2.4.0 test suite (`python tests/test_forge.py` or `pytest`)."""

import unittest
from test_forge_comprehensive import TestForgeCoreSuite  # noqa: F401

if __name__ == "__main__":
    unittest.main(verbosity=2)

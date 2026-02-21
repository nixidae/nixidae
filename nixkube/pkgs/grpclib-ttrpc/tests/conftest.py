"""pytest configuration and fixtures for grpclib_ttrpc tests."""

import asyncio
import os
import sys

import pytest_asyncio

# Ensure tests/ is on sys.path so helpers.py and dummy_pb2.py are importable.
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)


@pytest_asyncio.fixture
async def loop():
    return asyncio.get_running_loop()

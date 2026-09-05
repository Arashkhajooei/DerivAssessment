"""Shared pytest fixtures: paths to the real inputs and the broken/positive
fixture files used to exercise validation behaviour.
"""

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROKEN = os.path.join(ROOT, "tests", "fixtures", "broken")


@pytest.fixture
def real_inputs():
    return (
        os.path.join(ROOT, "kb.json"),
        os.path.join(ROOT, "queries.json"),
        os.path.join(ROOT, "candidate_answers.json"),
    )


@pytest.fixture
def broken_dir():
    return BROKEN

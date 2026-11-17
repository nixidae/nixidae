"""Code only a test in a guest runs: its coverage shows the guest's data
reached the host's pytest-cov (`checks.pytest-coverage`)."""


def where() -> str:
    return "in a guest"

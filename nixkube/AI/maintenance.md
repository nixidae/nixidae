# Maintenance tasks

## Match pyproject.toml dependencies with Nix dependencies
Ensure that all dependencies from Nix files are listed in pyproject.toml for correctness

## Imports
Python imports should always be at the top of the file if possible
structure:
1. SPDX header
2. module docstring
3. imports
4. if TYPE_CHECKING: imports
5. constants
6. code

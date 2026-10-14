"""The Rust ecosystem: a Cargo project, one node per unit of cargo's unit graph.

Cargo plans the build (`cargo build --unit-graph`), so features, build
scripts and host units are cargo's own decisions. The planner turns each unit
into a derivation, and `scripts/unit.py` builds it with `rustc`.
"""

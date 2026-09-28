"""Benchmark harness for the K3s IoT stack.

Replaces the monolithic run_test.sh and the eight near-duplicate bench_*.py
scripts. Corrects three measurement defects that made the paper-1 numbers
unreproducible; see docs/alternatives-considered.md and the module docstrings in
collect.py, report.py and preflight.py.
"""

__all__ = ["config", "collect", "report", "runner", "preflight"]

"""Scoring the query front-end.

The engine is the oracle: gold answers are produced by running the gold query,
never by writing a number down. So this golden set costs nothing to label, and
it cannot go stale -- change the engine and the gold answers change with it.
"""

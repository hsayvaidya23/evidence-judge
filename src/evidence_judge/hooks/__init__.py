"""Coding-agent hooks: judge the newest browser trace when an agent stops, and ask it to keep
working when the recorded evidence contradicts its final answer.

Every adapter fails open. A missing trace, a timeout, a crash or bad input never blocks the
agent; the user gets a one-line notice instead.
"""

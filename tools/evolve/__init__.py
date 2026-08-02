"""Experience-conditioned serving: intercept Harbor's model calls in-process.

`hook` is plumbing, `policy` is the algorithm under test, `replay` is the
offline loop you iterate in.
"""

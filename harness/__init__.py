"""Camoufox Firefox auto-update harness.

Detects a new Firefox release paired with a matching Playwright suite, rebases
the patch stack onto it, builds, and refuses to hand the result to a human
unless every gate produced evidence that it passed.
"""

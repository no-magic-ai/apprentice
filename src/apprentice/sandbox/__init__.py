"""Contained execution of generated code: one local Docker runner, trusted staging, hard bounds.

Generated implementations and Manim scenes never run in the apprentice
process or as host subprocesses. `docker.open_sandbox` verifies the one
explicitly configured backend (a local Unix-socket Docker context and a
concrete image built from `image/`) and refuses to run anything when it is
missing; `staging` reads the bytes to run from a caller-authorized root
without following links.
"""

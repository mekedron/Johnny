"""Tests for the voice-interrupt e2e harness (Johnny-2bw).

These tests exercise the harness *itself* — its frame synthesis, scripted
providers, transport pacing, and assertion evaluation — using the real
:class:`johnny.voice_pipeline.the legacy split pipeline`. They prove the harness wires
up correctly so the CLI (``python -m johnny.e2e.interrupt``) can be
trusted as a regression check for the interrupt feature.

The full harness CLI runs every scenario at production timing (a few
seconds per scenario); these tests target faster sub-checks.
"""

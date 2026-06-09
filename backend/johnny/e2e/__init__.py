"""Real-time end-to-end harnesses for the voice pipeline.

These are not unit tests. Each harness drives the real
:class:`johnny.voice_pipeline.the legacy split pipeline` with frame-by-frame production
pacing (≈20 ms async sleep per frame) so latency-budget assertions
(barge-in cut, transcript landing) actually mean something. Unit tests use
:mod:`tests.voice_pipeline.conftest`'s ``_BufferedTransport`` which races
through frames as fast as the consumer pulls — fine for behaviour checks,
useless for timing.

See :mod:`johnny.e2e.interrupt` for the voice-interrupt reproduction
harness (Johnny-2bw).
"""

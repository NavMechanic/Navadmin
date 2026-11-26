"""ATHENA N8 — minimal lifecycle receiver: unit genealogy and telemetry monitors from N4 releases.

This is the smallest slice of N8 the N4 handoff needs, not an N8 implementation. It accepts only an
N0 VERIFICATION_RELEASE that was RELEASED to N8 by an N0 manifest, whose stored payload still
hashes to the released SHA-256, whose verification status is VERIFIED, and whose N4 release has not
been invalidated. Each tested unit serial becomes a genealogy record locked to the design revision,
and each guard-banded characteristic becomes a telemetry monitor with its acceptance limits.
"""
N8_VERSION = "0.1.0"

#!/usr/bin/env python3
"""Run with: python oay/test_hybrid_bridge.py (no production key required)."""
import base64
import hashlib
import json
import os
import sys
sys.path.insert(0, "oay")
from hybrid_bridge import recipient_secret, public_bytes, seal, open_packet
from cryptography.exceptions import InvalidTag

old = os.environ.get("OAY_AES256_KEY_B64")
try:
    os.environ["OAY_AES256_KEY_B64"] = base64.b64encode(os.urandom(32)).decode()
    priv = recipient_secret()
    row = {"cols": [1, 3, 5, 8, 11, 17], "d": 0, "direction": "KG VAR",
           "sig": hashlib.sha256(b"fake synthetic row").hexdigest()}
    raw = (json.dumps(row, sort_keys=True) + "\n").encode()
    payload = seal(raw, public_bytes(priv), hashlib.sha256(b"synthetic-gate").hexdigest(), 0)
    assert open_packet(payload, priv) == raw
    bad = dict(payload)
    bad["raw_sha256"] = "0" * 64
    try:
        open_packet(bad, priv)
        raise AssertionError("Digest tampering should fail")
    except ValueError:
        pass
    raw_a = bytearray(base64.b64decode(payload["ciphertext"]))
    raw_a[-1] ^= 1
    bad = dict(payload)
    bad["ciphertext"] = base64.b64encode(raw_a).decode()
    try:
        open_packet(bad, priv)
        raise AssertionError("Ciphertext tampering should fail")
    except InvalidTag:
        pass
    print("Hybrid bridge synthetic crypto round-trip and tamper tests PASSED")
finally:
    if old is None:
        os.environ.pop("OAY_AES256_KEY_B64", None)
    else:
        os.environ["OAY_AES256_KEY_B64"] = old

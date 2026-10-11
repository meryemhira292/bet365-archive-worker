#!/usr/bin/env python3
"""Independent encrypted hybrid inbox -> OAY V9 exact worker.

Runs beside production, never edits the existing V9 Stage2 state or gate.
Its own AES-GCM-encrypted GitHub artifact preserves the full append-only
hybrid queue and all exact results. 2026 is never opened; no auto-approval.
"""
from __future__ import annotations
import base64
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import urllib.request
import zipfile
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from hybrid_bridge import open_packet, recipient_secret, rows_from_bytes

ROOT = pathlib.Path("private")
INBOX = pathlib.Path("oay/hybrid_inbox")
ARTIFACT_PREFIX = "oay-hybrid-state-"
SEAL = "SEALED_NOT_READ"
TRAIN_SHA = "2e9c70df01e3cd9f7d3f30128000118a76132968f5ea47c242f75fcaf79cd622"


def sha(b):
    return hashlib.sha256(b).hexdigest()


def digest_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def json_bytes(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def line_bytes(rows):
    return b"".join(json_bytes(r) + b"\n" for r in rows)


def pack(files):
    manifest = {name: sha(data) for name, data in files.items()}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name, data in files.items():
            z.writestr(name, data)
        z.writestr("MANIFEST_SHA256.json", json_bytes(manifest))
    return buf.getvalue()


def unpack(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        manifest = json.loads(z.read("MANIFEST_SHA256.json"))
        if set(manifest) | {"MANIFEST_SHA256.json"} != names:
            raise ValueError("Checkpoint file list mismatch")
        for name, digest in manifest.items():
            if sha(z.read(name)) != digest:
                raise ValueError("Checkpoint manifest mismatch " + name)
        return {name: z.read(name) for name in manifest}


def github_get(url):
    token = os.environ["GH_TOKEN"]
    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            nxt = super().redirect_request(req, fp, code, msg, headers, newurl)
            if nxt and urlparse(newurl).netloc != urlparse(req.full_url).netloc:
                nxt.headers.pop("Authorization", None)
                nxt.unredirected_hdrs.pop("Authorization", None)
            return nxt
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.build_opener(SafeRedirect()).open(req, timeout=120) as response:
        return response.read()


def restore(key):
    api = "https://api.github.com/repos/" + os.environ["GITHUB_REPOSITORY"]
    artifacts = []
    for page in (1, 2, 3):
        data = json.loads(github_get(api + f"/actions/artifacts?per_page=100&page={page}"))
        entries = data.get("artifacts", [])
        artifacts.extend(a for a in entries if a["name"].startswith(ARTIFACT_PREFIX) and not a["expired"])
        if len(entries) < 100:
            break
    if not artifacts:
        return None
    artifacts.sort(key=lambda a: (a["created_at"], a["id"]), reverse=True)
    archive = github_get(api + "/actions/artifacts/" + str(artifacts[0]["id"]) + "/zip")
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        ciphertext = z.read("hybrid_state.enc")
    plaintext = AESGCM(key).decrypt(ciphertext[:12], ciphertext[12:], None)
    return unpack(plaintext)


def main():
    key = base64.b64decode(os.environ["OAY_AES256_KEY_B64"], validate=True)
    if len(key) != 32:
        raise RuntimeError("Invalid repository AES-256 secret")
    priv = recipient_secret()
    if not (ROOT / "GATE.zip").is_file() or not (ROOT / "production_runner.py").is_file():
        raise RuntimeError("Canonical private bundle has not been decrypted")
    base_sha = digest_file(ROOT / "GATE.zip")
    base_signatures = set()
    with zipfile.ZipFile(ROOT / "GATE.zip") as z:
        for line in z.open("EXACT_QUEUE.jsonl"):
            if line.strip():
                base_signatures.add(json.loads(line)["sig"])
    old = restore(key)
    if old is None:
        ledger = {"schema": 1, "base_gate_sha256": base_sha, "files": {}, "train_sha256": TRAIN_SHA}
        queue = []
        results = []
    else:
        ledger = json.loads(old["LEDGER.json"])
        if ledger["schema"] != 1 or ledger["base_gate_sha256"] != base_sha or ledger["train_sha256"] != TRAIN_SHA:
            raise RuntimeError("Historic checkpoint incompatible; refusing reset")
        queue = [json.loads(x) for x in old["HYBRID_QUEUE.jsonl"].splitlines() if x]
        results = [json.loads(x) for x in old["EXACT_RESULTS.jsonl"].splitlines() if x]
        if len({r["sig"] for r in queue}) != len(queue) or [r["sig"] for r in results] != [r["sig"] for r in queue[:len(results)]]:
            raise ValueError("Corrupt queue/result prefix")
        if any(r.get("2026_holdout") != "SEALED" or r.get("approved") for r in results):
            raise ValueError("Old checkpoint contains unexpected results")
    already = {r["sig"] for r in queue}
    new = 0
    for path in sorted(INBOX.glob("*.json")):
        raw_packet = path.read_bytes()
        packet_sha = sha(raw_packet)
        if path.name in ledger["files"]:
            if ledger["files"][path.name] != packet_sha:
                raise RuntimeError("Published encrypted packet was modified: " + path.name)
            continue
        packet = json.loads(raw_packet)
        chunk = open_packet(packet, priv)
        for row in rows_from_bytes(chunk):
            if row["sig"] not in base_signatures and row["sig"] not in already:
                queue.append(row)
                already.add(row["sig"])
                new += 1
        ledger["files"][path.name] = packet_sha
    if not queue:
        print("HYBRID_IDLE: no candidate packets received", flush=True)
        return
    queue_raw = line_bytes(queue)
    gate_report = json_bytes({
        "stage": "hybrid_gate_append_only", "queue_total": len(queue),
        "base_gate_sha256": base_sha, "holdout2026": SEAL
    })
    gate = ROOT / "HYBRID_GATE.zip"
    gate.write_bytes(pack({"EXACT_QUEUE.jsonl": queue_raw, "GATE_REPORT.json": gate_report}))
    gate_sha = digest_file(gate)
    if any(r["sig"] in base_signatures for r in queue):
        raise RuntimeError("Hybrid/base queue overlap")
    seed_report = json_bytes({
        "stage": "exact_v9", "total_exact": len(results),
        "gate_sha256": gate_sha, "training_sha256": TRAIN_SHA,
        "holdout2026": SEAL, "new_approved": 0
    })
    seed = ROOT / "HYBRID_SEED.zip"
    seed.write_bytes(pack({"EXACT_RESULTS.jsonl": line_bytes(results), "EXACT_REPORT.json": seed_report}))
    if len(results) < len(queue):
        subprocess.run([
            sys.executable, str(ROOT / "production_runner.py"),
            "--train", str(ROOT / "TRAIN_2018_2025.npz"),
            "--gate", str(gate), "--seed", str(seed),
            "--outdir", str(ROOT / "hybrid_checkpoints"),
            "--workers", "4", "--block", "5000", "--seconds", "2850"
        ], check=True)
        state_path = ROOT / "hybrid_checkpoints" / "STATE.json"
        if state_path.is_file():
            run_state = json.loads(state_path.read_text())
            final_zip = ROOT / "hybrid_checkpoints" / run_state["current"]
        else:
            final_zip = seed
        verified = unpack(final_zip.read_bytes())
        report = json.loads(verified["EXACT_REPORT.json"])
        results_new = [json.loads(x) for x in verified["EXACT_RESULTS.jsonl"].splitlines() if x]
        if report["gate_sha256"] != gate_sha:
            raise ValueError("Exact runner gate SHA mismatch")
        if [r["sig"] for r in results_new] != [r["sig"] for r in queue[:len(results_new)]]:
            raise ValueError("Runner exact result is not a queue prefix")
        delta = len(results_new) - len(results)
        if delta < 0:
            raise ValueError("Exact test count decreased")
        results = results_new
    else:
        delta = 0
    ledger["queue_total"] = len(queue)
    ledger["exact_completed"] = len(results)
    ledger["holdout2026"] = SEAL
    archive = pack({
        "LEDGER.json": json_bytes(ledger),
        "HYBRID_QUEUE.jsonl": queue_raw,
        "EXACT_RESULTS.jsonl": line_bytes(results),
    })
    nonce = os.urandom(12)
    pathlib.Path("hybrid_state.enc").write_bytes(nonce + AESGCM(key).encrypt(nonce, archive, None))
    print(json.dumps({
        "status": "CHECKPOINT_ENCRYPTED",
        "new_inbox_unique": new,
        "new_exact": delta,
        "exact_completed": len(results),
        "queue_total": len(queue),
        "remaining": len(queue) - len(results),
        "approved": 0,
        "holdout2026": SEAL,
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

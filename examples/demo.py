#!/usr/bin/env python3
"""End-to-end demonstration of the Gene Annotation Alignment API.

Uses only the Python standard library.  Start the stack first:

    docker compose up --build            # or run both services locally

then:

    python3 examples/demo.py

Scenario map (see examples/README.md for the file manifest):

  1. Shared lookup, cancel one batch  -> batches A and B share genes; B is
     cancelled mid-flight; A completes normally; the source saw exactly one
     HTTP request per shared gene.
  2. Cancel everything, re-query at once -> batch C is cancelled while its
     lookups are in flight (aborting them); batch D with the same genes is
     submitted immediately afterwards and triggers brand-new requests.
  3. Source failure                   -> a non-404 source error fails the
     whole batch; no download is offered.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("API_URL", "http://localhost:8000")
SOURCE = os.environ.get("SOURCE_URL", "http://localhost:8001")
HERE = os.path.dirname(os.path.abspath(__file__))

FAILED = False


# ----------------------------------------------------------------------
# tiny HTTP helpers
# ----------------------------------------------------------------------


def request(method, url, body=None, headers=None, expect=(200,)):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, payload = resp.status, resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
        ctype = exc.headers.get("Content-Type", "") if exc.headers else ""
    if expect and status not in expect:
        raise SystemExit(f"FAIL: {method} {url} -> HTTP {status}: {payload[:400]!r}")
    if "application/json" in ctype:
        return status, json.loads(payload)
    return status, payload.decode()


def upload_csv(path):
    with open(path, "rb") as handle:
        data = handle.read()
    _, info = request(
        "POST",
        f"{API}/batches",
        body=data,
        headers={"Content-Type": "text/csv"},
        expect=(202,),
    )
    return info


def batch_info(batch_id):
    return request("GET", f"{API}/batches/{batch_id}")[1]


def wait_for(batch_id, wanted, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = batch_info(batch_id)
        if info["status"] == wanted:
            return info
        if info["status"] in ("failed", "cancelled") and info["status"] != wanted:
            raise SystemExit(f"batch {batch_id} unexpectedly {info['status']}: {info.get('error')}")
        time.sleep(0.2)
    raise SystemExit(f"timeout waiting for batch {batch_id} to become {wanted}")


def source_stats():
    return request("GET", f"{SOURCE}/stats")[1]


def reset_stats():
    request("POST", f"{SOURCE}/stats/reset")


def check(label, condition, detail=""):
    global FAILED
    mark = "ok " if condition else "FAIL"
    print(f"    [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        FAILED = True


def banner(text):
    print(f"\n=== {text} " + "=" * max(4, 66 - len(text)))


# ----------------------------------------------------------------------
# demo
# ----------------------------------------------------------------------


def main():
    request("GET", f"{API}/healthz")
    stats = source_stats()
    delay = float(stats["delay_seconds"])
    settle = max(0.3, delay * 0.25)  # cancel while lookups are still in flight
    print(f"API {API} | source {SOURCE} | source delay {delay}s per lookup")

    # ------------------------------------------------------------------
    banner("1. shared lookup across batches, cancel one of them")
    reset_stats()
    a = upload_csv(f"{HERE}/batch_a.csv")
    b = upload_csv(f"{HERE}/batch_b.csv")
    print(f"    batch A = {a['batch_id']}  batch B = {b['batch_id']}")
    time.sleep(settle)
    inflight = request("GET", f"{API}/debug/inflight")[1]
    shared = {q["gene"]: q["subscribers"] for q in inflight}
    print(f"    in-flight lookups while both run: {shared}")
    check(
        "ENSG00000141510 looked up once for both batches",
        shared.get("ENSG00000141510") == 2,
        "one query, two subscribers",
    )
    request("POST", f"{API}/batches/{b['batch_id']}/cancel")
    info_b = batch_info(b["batch_id"])
    check("batch B cancelled immediately", info_b["status"] == "cancelled")
    status, _ = request("GET", f"{API}/batches/{b['batch_id']}/download", expect=(409,))
    check("cancelled batch B offers no download (HTTP 409)", status == 409)

    info_a = wait_for(a["batch_id"], "completed")
    check(
        "batch A completed normally",
        info_a["resolved_genes"] == info_a["unique_genes"],
        f"{info_a['resolved_genes']}/{info_a['unique_genes']} genes resolved",
    )
    stats = source_stats()
    per_gene = stats["requests_per_gene"]
    print(f"    source requests per gene: {per_gene}")
    check(
        "shared genes hit the source exactly once",
        per_gene.get("ENSG00000141510") == 1 and per_gene.get("ENSG00000012048") == 1,
        "TP53 + BRCA1 coalesced",
    )
    check(
        "B-only genes were queried once, then aborted on cancel",
        per_gene.get("ENSG00000139618") == 1 and per_gene.get("ENSG00000133703") == 1,
    )
    _, csv_a = request("GET", f"{API}/batches/{a['batch_id']}/download")
    print("    batch A result CSV:")
    for line in csv_a.strip().splitlines():
        print(f"      {line}")
    check(
        "original ids, row order and duplicate row preserved",
        "ENSG00000141510.17,TP53,17" in csv_a
        and csv_a.strip().splitlines().count("ENSG00000141510.17,TP53,17") == 2
        and "ENSG00000999999.1,NA,NA" in csv_a,
        "duplicate TP53 rows + unknown gene marked NA",
    )

    # ------------------------------------------------------------------
    banner("2. cancel everything, re-query immediately")
    reset_stats()
    c = upload_csv(f"{HERE}/batch_c.csv")
    print(f"    batch C = {c['batch_id']}")
    time.sleep(settle)
    request("POST", f"{API}/batches/{c['batch_id']}/cancel")
    check("batch C cancelled", batch_info(c["batch_id"])["status"] == "cancelled")
    d = upload_csv(f"{HERE}/batch_c.csv")  # same genes, brand-new batch
    print(f"    batch D = {d['batch_id']} (same file, submitted right after the cancel)")
    info_d = wait_for(d["batch_id"], "completed")
    check("batch D completed", info_d["status"] == "completed")
    per_gene = source_stats()["requests_per_gene"]
    print(f"    source requests per gene: {per_gene}")
    check(
        "cancelled lookups were aborted and re-issued for batch D",
        per_gene.get("ENSG00000130203") == 2 and per_gene.get("ENSG00000198947") == 2,
        "no caching: one aborted attempt + one fresh request each",
    )
    check(
        "batch C stayed cancelled and has no download",
        batch_info(c["batch_id"])["status"] == "cancelled"
        and request("GET", f"{API}/batches/{c['batch_id']}/download", expect=(409,))[0] == 409,
    )
    _, csv_d = request("GET", f"{API}/batches/{d['batch_id']}/download")
    print("    batch D result CSV:")
    for line in csv_d.strip().splitlines():
        print(f"      {line}")
    check(
        "late results of the aborted queries did not leak into batch D",
        "ENSG00000130203.19,APOE,19" in csv_d and "ENSG00000198947.11,DMD,X" in csv_d,
    )

    # ------------------------------------------------------------------
    banner("3. non-404 source failure fails the whole batch")
    f = upload_csv(f"{HERE}/batch_fail.csv")
    print(f"    batch F = {f['batch_id']} (contains a gene that makes the source return 500)")
    info_f = wait_for(f["batch_id"], "failed")
    check("batch failed as a whole", info_f["status"] == "failed", info_f.get("error") or "")
    status, _ = request("GET", f"{API}/batches/{f['batch_id']}/download", expect=(409,))
    check("failed batch offers no download (HTTP 409)", status == 409)

    print()
    if FAILED:
        print("DEMO FAILED - see [FAIL] lines above")
        return 1
    print("DEMO OK - all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Persona-test audit (Tab): grief path + banned words + tell-anyone.

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_grief.py

Covers:
- detect_grief: bereavement disclosures fire (EN + HE); ordinary
  talk, near-misses and possessive-object losses never fire.
- E2E: a grief message gets the FIXED opening line (EN / HE) in
  front of the model continuation, crisis stays False, no banner,
  no hotline footer; the streamed form emits the opening first.
- A grief message that also carries risk takes the CRISIS path
  (frozen response, model never called) — grief never masks risk.
- A non-grief message is untouched (no opening prepended).
- Audit (a): SYSTEM_PROMPT carries the identity-language rule;
  the fixed grief strings themselves are banned-word clean.
- Audit (c): the "won't tell anyone" reassurance is on the first
  screen (intake) and the privacy page, EN + HE; SYSTEM_PROMPT
  carries the reassurance rule. The first screen still shows
  "not a substitute for professional care" exactly once.
- Frozen crisis strings byte-identical (hash pins).
"""
import hashlib
import json
import os
import sys
import uuid

os.environ["CRISIS_CLASSIFIER"] = "off"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as solace  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

failures = []

# ---------- frozen strings: byte-identical -------------------------
for name, want in (
    ("FIXED_CRISIS_RESPONSE",
     "718f7c98131223578dd68c441f90cad7f72404d9abf3de5952bef39581aa5250"),
    ("FIXED_CRISIS_RESPONSE_HE",
     "d9306c9af9205559b745bfbbf0b9b1ad26df027dc2376cd8325aa569d2c1365d"),
):
    got = hashlib.sha256(getattr(solace, name).encode()).hexdigest()
    if got != want:
        failures.append("FROZEN %s changed: %s" % (name, got))

print("frozen: crisis strings byte-identical")

# ---------- detect_grief ---------------------------------------------
POS = [
    "My mom died last week",
    "my father passed away yesterday",
    "I lost my brother in an accident",
    "Mom just died",
    "my grandmother's funeral is tomorrow",
    "we buried my dad on Sunday",
    "my dog died this morning",
    "I lost my wife to cancer",
    "אמא שלי נפטרה אתמול",
    "איבדתי את אבא שלי",
    "הסבא שלי נפטר",
]
NEG = [
    "I died laughing at that joke",
    "my dad passed his driving test",
    "my mom is a nurse",
    "I lost my keys again",
    "my brother is getting married",
    "my wife almost died laughing",
    "my mom nearly died last year but she recovered",
    "the funeral scene in the movie was sad",
    "I lost my friend's number",
    "נפטרתי מהעבודה סוף סוף",
    "Work has been draining me lately.",
]
for t in POS:
    if not solace.detect_grief(t):
        failures.append("GRIEF detector missed: %r" % t)
for t in NEG:
    if solace.detect_grief(t):
        failures.append("GRIEF detector false positive: %r" % t)

print("detector: %d disclosures fire, %d ordinary lines don't"
      % (len(POS), len(NEG)))

# ---------- model stubs ----------------------------------------------
stream_calls = {"n": 0}
sync_calls = {"n": 0}


def stub_stream(system, messages):
    stream_calls["n"] += 1
    yield "Stub continuation."


def stub_sync(model, system, messages, max_tokens=1024):
    sync_calls["n"] += 1
    return "Stub continuation."


solace._anthropic_stream_chunks = stub_stream
solace.call_anthropic = stub_sync

client = TestClient(solace.app)
client.__enter__()


def new_user():
    uid = "test-" + uuid.uuid4().hex[:12]
    client.post("/api/profile",
                json={"user_id": uid, "name": "", "notes": ""})
    return uid


def sse_events(raw):
    evts = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            try:
                evts.append(json.loads(line[5:]))
            except Exception:
                pass
    return evts


# ---------- E2E: grief turn, EN (stream) ------------------------------
uid = new_user()
evts = sse_events(client.post("/api/chat/stream", json={
    "user_id": uid, "message": "My mom died last week.",
}).text)
deltas = [e.get("text", "") for e in evts if e.get("type") == "delta"]
done = next((e for e in evts if e.get("type") == "done"), {})
if not deltas or not deltas[0].startswith(solace.GRIEF_OPENING[:30]):
    failures.append("GRIEF stream does not open with the fixed line: %r"
                    % (deltas[:1],))
reply = done.get("reply", "")
if not reply.startswith(solace.GRIEF_OPENING):
    failures.append("GRIEF reply does not start with GRIEF_OPENING: %r"
                    % reply[:120])
if "Stub continuation." not in reply:
    failures.append("GRIEF reply lost the model continuation")
if done.get("crisis"):
    failures.append("GRIEF turn flagged crisis")
if done.get("banner"):
    failures.append("GRIEF turn raised the crisis banner")
if "988" in reply or "741741" in reply:
    failures.append("GRIEF reply carries a crisis hotline footer")

print("e2e EN: fixed opening + continuation, no crisis machinery")

# ---------- E2E: grief turn, HE (non-stream) ---------------------------
uid = new_user()
r = client.post("/api/chat", json={
    "user_id": uid, "message": "אמא שלי נפטרה אתמול",
}).json()
if not r.get("reply", "").startswith(solace.GRIEF_OPENING_HE):
    failures.append("GRIEF HE reply does not start with the HE opening: %r"
                    % r.get("reply", "")[:120])
if r.get("crisis"):
    failures.append("GRIEF HE turn flagged crisis")
if "1201" in r.get("reply", "") or "101" in r.get("reply", ""):
    failures.append("GRIEF HE reply carries hotline numbers")

print("e2e HE: fixed Hebrew opening, no crisis machinery")

# ---------- E2E: grief + risk takes the crisis path --------------------
uid = new_user()
before = stream_calls["n"] + sync_calls["n"]
body = client.post("/api/chat/stream", json={
    "user_id": uid,
    "message": "My mom died last week and I want to kill myself",
}).text
if '"crisis": true' not in body and '"crisis":true' not in body:
    failures.append("GRIEF+RISK did not flag crisis")
if solace.FIXED_CRISIS_RESPONSE[:40] not in body:
    failures.append("GRIEF+RISK reply is not the frozen crisis response")
if solace.GRIEF_OPENING[:30] in body:
    failures.append("GRIEF+RISK leaked the grief opening")
if stream_calls["n"] + sync_calls["n"] != before:
    failures.append("GRIEF+RISK called the model")

print("e2e: grief + risk -> frozen crisis path, model never called")

# ---------- E2E: ordinary message untouched -----------------------------
uid = new_user()
r = client.post("/api/chat", json={
    "user_id": uid, "message": "Work has been draining me lately.",
}).json()
if solace.GRIEF_OPENING[:30] in r.get("reply", ""):
    failures.append("ORDINARY turn got the grief opening")
if r.get("reply") != "Stub continuation.":
    failures.append("ORDINARY reply changed: %r" % r.get("reply", "")[:80])

print("e2e: ordinary turn untouched")

# ---------- audit (a): identity language --------------------------------
if "IDENTITY LANGUAGE" not in solace.SYSTEM_PROMPT:
    failures.append("AUDIT(a) SYSTEM_PROMPT lacks the identity-language rule")
for w in ("therapy", "therapist", "psychologist", "counselor",
          "treatment", "diagnose", "cure"):
    if w in (solace.GRIEF_OPENING + solace.GRIEF_FALLBACK).lower():
        failures.append("AUDIT(a) grief strings contain banned word: %s" % w)
for w in ("פסיכולוג", "טיפול", "מטפל"):
    if w in (solace.GRIEF_OPENING_HE + solace.GRIEF_FALLBACK_HE):
        failures.append("AUDIT(a) HE grief strings contain banned word: %s" % w)

print("audit(a): identity-language rule in prompt; grief strings clean")

# ---------- audit (c): tell-anyone reassurance ---------------------------
html = client.get("/").text
if "Solace won't tell anyone what you share here." not in html:
    failures.append("AUDIT(c) first-screen reassurance missing")
if html.count("not a substitute for professional care") != 1:
    failures.append("AUDIT(c) care line count on first screen != 1: %d"
                    % html.count("not a substitute for professional care"))
priv = client.get("/privacy").text
if "Solace won't tell anyone what you share here." not in priv:
    failures.append("AUDIT(c) privacy page EN reassurance missing")
if "לא תספר לאף אחד" not in priv:
    failures.append("AUDIT(c) privacy page HE reassurance missing")
if "not going to tell anyone" not in solace.SYSTEM_PROMPT:
    failures.append("AUDIT(c) SYSTEM_PROMPT lacks the reassurance rule")

print("audit(c): reassurance on first screen + privacy (EN+HE) + prompt")

if failures:
    print("\nFAILURES (%d):" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL GRIEF / AUDIT CHECKS PASSED")

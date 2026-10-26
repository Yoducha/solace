"""Overdose routing (Tab ruling, Oct 26): a COMPLETED overdose is a
medical emergency — the reply leads with call 911 (מד״א 101 in
Hebrew), never with the suicide question. Plans and ordinary
medication talk keep their existing paths. Frozen crisis strings
byte-identical.

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_emergency.py
"""
import hashlib
import os
import sys
import uuid

os.environ["CRISIS_CLASSIFIER"] = "off"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as solace  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

failures = []

# ---------- frozen strings untouched ----------------------------------
for name, want in (
    ("FIXED_CRISIS_RESPONSE",
     "718f7c98131223578dd68c441f90cad7f72404d9abf3de5952bef39581aa5250"),
    ("FIXED_CRISIS_RESPONSE_HE",
     "d9306c9af9205559b745bfbbf0b9b1ad26df027dc2376cd8325aa569d2c1365d"),
):
    got = hashlib.sha256(getattr(solace, name).encode()).hexdigest()
    if got != want:
        failures.append("FROZEN %s changed: %s" % (name, got))

# ---------- detector: positives / negatives ----------------------------
POS = [
    "I just took a whole bottle of pills.",
    "I took all my pills an hour ago",
    "I swallowed a bottle of pills",
    "I took 20 pills",
    "I took too many pills",
    "I took a handful of tablets",
    "I overdosed",
    "I took an overdose of my meds",
    "בלעתי את כל הכדורים",
    "לקחתי יותר מדי כדורים",
    "זו מנת יתר",
    # Tab round 2 (Oct 26): his exact live string was missed.
    "בלעתי הרבה כדורים",
    "לקחתי המון גלולות",
    "בלעתי מלא כדורים",
    "זו הרעלת תרופות",
]
NEG = [
    "I'm going to overdose tonight",
    "I have pills saved up",
    "I took my pills this morning like always",
    "I took two aspirin for a headache",
    "Work has been draining me lately.",
    "I want to die",
    # Hebrew medication talk without a quantity word stays safe,
    # and present-tense chronic use is not a completed overdose.
    "לקחתי את הכדורים שלי הבוקר",
    "אני לוקח הרבה כדורים כל יום",
    "לקחתי את התרופה שלי כמו תמיד",
]
for t in POS:
    if not solace.detect_overdose(t):
        failures.append("OVERDOSE detector missed: %r" % t)
for t in NEG:
    if solace.detect_overdose(t):
        failures.append("OVERDOSE detector false positive: %r" % t)

print("detector: %d completed-ingestion lines fire, %d don't"
      % (len(POS), len(NEG)))

# ---------- E2E ---------------------------------------------------------
calls = {"n": 0}


def stub_sync(model, system, messages, max_tokens=1024):
    calls["n"] += 1
    return "Stub reply."


def stub_stream(system, messages):
    calls["n"] += 1
    yield "Stub reply."


solace.call_anthropic = stub_sync
solace._anthropic_stream_chunks = stub_stream

client = TestClient(solace.app)
client.__enter__()


def new_user():
    uid = "test-" + uuid.uuid4().hex[:12]
    client.post("/api/profile",
                json={"user_id": uid, "name": "", "notes": ""})
    return uid


# English overdose: 911 leads, suicide question does not, no model.
uid = new_user()
calls["n"] = 0
r = client.post("/api/chat", json={
    "user_id": uid,
    "message": "I just took a whole bottle of pills."}).json()
if not r["crisis"] or not r["banner"]:
    failures.append("E2E overdose not latched/bannered: %r" % r)
if not r["reply"].startswith("Call 911"):
    failures.append("E2E overdose does not lead with 911: %r"
                    % r["reply"][:80])
if "Are you thinking about acting" in r["reply"]:
    failures.append("E2E overdose led with the suicide question")
if calls["n"] != 0:
    failures.append("E2E overdose called the model (%d)" % calls["n"])

# Hebrew overdose: מד״א 101 leads.
uid = new_user()
r = client.post("/api/chat", json={
    "user_id": uid, "message": "בלעתי את כל הכדורים"}).json()
if not r["crisis"]:
    failures.append("E2E HE overdose not crisis: %r" % r)
if "101" not in r["reply"][:60] or "מד״א" not in r["reply"]:
    failures.append("E2E HE overdose does not lead with מד״א 101: %r"
                    % r["reply"][:80])
if "911" in r["reply"]:
    failures.append("E2E HE overdose mentions 911: %r" % r["reply"])

# Tab round 2 exact string: "בלעתי הרבה כדורים" must lead with
# מד״א 101 — not the frozen suicide question.
uid = new_user()
r = client.post("/api/chat", json={
    "user_id": uid, "message": "בלעתי הרבה כדורים"}).json()
if not r["crisis"] or not r["banner"]:
    failures.append("E2E HE הרבה כדורים not latched/bannered: %r" % r)
if "101" not in r["reply"][:60] or "מד״א" not in r["reply"]:
    failures.append("E2E HE הרבה כדורים does not lead with מד״א 101: %r"
                    % r["reply"][:80])
if "האם אתה חושב לפעול" in r["reply"]:
    failures.append("E2E HE הרבה כדורים led with the suicide question")

# A plan (not completed) keeps the FROZEN crisis response verbatim.
uid = new_user()
r = client.post("/api/chat", json={
    "user_id": uid,
    "message": "I'm going to overdose tonight"}).json()
if r["reply"] != solace.FIXED_CRISIS_RESPONSE:
    failures.append("E2E plan case left the frozen path: %r"
                    % r["reply"][:80])

# Ordinary medication talk: no crisis, normal model reply.
uid = new_user()
r = client.post("/api/chat", json={
    "user_id": uid,
    "message": "I took my pills this morning like always."}).json()
if r["crisis"]:
    failures.append("E2E medication talk triggered crisis: %r" % r)

print("E2E: EN 911-first, HE מד״א 101-first, plan stays frozen, "
      "medication talk untouched")

if failures:
    print("\nFAILURES (%d):" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL EMERGENCY ROUTING CHECKS PASSED")

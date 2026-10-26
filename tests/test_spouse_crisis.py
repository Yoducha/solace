"""Widow route (Tab ruling, Oct 26): a spouse-loss disclosure whose
message also carries crisis risk gets a reply that names the loss
(one line about her husband / his wife), keeps the safety question,
the resources and the banner — and does NOT use the generic
"tell me what's going on" closer. The frozen crisis strings are
untouched and still serve every other crisis.

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_spouse_crisis.py
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

for name, want in (
    ("FIXED_CRISIS_RESPONSE",
     "718f7c98131223578dd68c441f90cad7f72404d9abf3de5952bef39581aa5250"),
    ("FIXED_CRISIS_RESPONSE_HE",
     "d9306c9af9205559b745bfbbf0b9b1ad26df027dc2376cd8325aa569d2c1365d"),
):
    got = hashlib.sha256(getattr(solace, name).encode()).hexdigest()
    if got != want:
        failures.append("FROZEN %s changed: %s" % (name, got))

# ---------- detector -----------------------------------------------------
CASES = {
    "I'm a widow, 47 years old": "husband",
    "I'm a widower now": "wife",
    "my husband died last spring": "husband",
    "I lost my wife to cancer": "wife",
    "my late husband's chair is still there": "husband",
    "אני אלמנה כבר שנה": "husband",
    "איבדתי את אשתי": "wife",
    "my wife is a nurse": None,
    "my husband is at work": None,
    "Work has been draining me lately.": None,
}
for text, want in CASES.items():
    got = solace.detect_spouse_loss(text)
    if got != want:
        failures.append("SPOUSE detector %r -> %r, want %r"
                        % (text, got, want))

print("detector: spouse loss named correctly, ordinary lines clean")

# ---------- E2E ------------------------------------------------------------
calls = {"n": 0}


def stub_sync(model, system, messages, max_tokens=1024):
    calls["n"] += 1
    return "Stub reply."


def stub_stream(system, messages):
    calls["n"] += 1
    yield "Stub reply."


solace.call_anthropic = stub_sync
solace._anthropic_stream_chunks = stub_stream
# Tab's exact scenario only trips the (stubbed-off) classifier layer
# in production — stand it in so the local run matches his test.
solace.classify_risk = lambda text: "point" in text

client = TestClient(solace.app)
client.__enter__()


def new_user():
    uid = "test-" + uuid.uuid4().hex[:12]
    client.post("/api/profile",
                json={"user_id": uid, "name": "", "notes": ""})
    return uid


# Tab's exact scenario.
uid = new_user()
calls["n"] = 0
r = client.post("/api/chat", json={
    "user_id": uid,
    "message": ("I'm a widow, 47 years. Since he died I just "
                "don't see the point anymore.")}).json()
rep = r["reply"]
if not r["crisis"] or not r["banner"]:
    failures.append("WIDOW not crisis/banner: %r" % r)
if "your husband" not in rep:
    failures.append("WIDOW reply lacks the husband line: %r" % rep)
if "988" not in rep:
    failures.append("WIDOW reply lost 988: %r" % rep)
if "Are you thinking about acting on these thoughts tonight?" not in rep:
    failures.append("WIDOW reply lost the safety question: %r" % rep)
if "tell me what's going on" in rep:
    failures.append("WIDOW reply kept the generic closer: %r" % rep)
if rep == solace.FIXED_CRISIS_RESPONSE:
    failures.append("WIDOW reply is just the frozen reply")
if calls["n"] != 0:
    failures.append("WIDOW path called the model (%d)" % calls["n"])

# Keyword-driven widow crisis (classifier not needed).
uid = new_user()
r = client.post("/api/chat", json={
    "user_id": uid,
    "message": "I'm a widower and I want to die"}).json()
if "your wife" not in r["reply"] or not r["crisis"]:
    failures.append("WIDOWER reply wrong: %r" % r["reply"])

# Non-spouse crisis: frozen reply verbatim, unchanged.
uid = new_user()
r = client.post("/api/chat", json={
    "user_id": uid, "message": "I want to die"}).json()
if r["reply"] != solace.FIXED_CRISIS_RESPONSE:
    failures.append("PLAIN crisis left the frozen reply: %r"
                    % r["reply"][:80])

# Spouse loss WITHOUT risk: no crisis latch from this route.
uid = new_user()
solace.classify_risk = lambda text: False
r = client.post("/api/chat", json={
    "user_id": uid,
    "message": "I'm a widow. Some days are just heavy."}).json()
if r["crisis"]:
    failures.append("CALM widow message latched crisis: %r" % r)

print("E2E: widow + widower named, safety kept, generic closer gone, "
      "plain crisis still frozen, calm widow untouched")

if failures:
    print("\nFAILURES (%d):" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL SPOUSE-CRISIS CHECKS PASSED")

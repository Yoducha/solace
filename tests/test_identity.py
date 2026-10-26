"""Identity questions + referral wording (Tab ruling, Oct 26):
"Is this therapy?" and "Are you a psychologist?" get a plain
deterministic "No" — never a model reply — and the app's own
referral phrasing is the filter-safe "a doctor or a licensed
mental health professional", so referrals are never cut.

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_identity.py
"""
import os
import sys
import uuid

os.environ["CRISIS_CLASSIFIER"] = "off"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as solace  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

failures = []

# ---------- detector -----------------------------------------------------
POS = {
    "Is this therapy?": "therapy",
    "is this therapy": "therapy",
    "Is that counseling?": "therapy",
    "האם זה טיפול?": "therapy",
    "זה טיפול?": "therapy",
    "Are you a psychologist?": "professional",
    "Are you a therapist": "professional",
    "are you my therapist?": "professional",
    "אתה פסיכולוג?": "professional",
    "האם את פסיכולוגית?": "professional",
    "אתה מטפל?": "professional",
}
NEG = [
    "I need therapy, honestly.",
    "My therapist said I should journal.",
    "Therapy helped my sister a lot.",
    "What is a psychologist?",
    "אני הולך לטיפול מחר",
    "Work has been overwhelming lately.",
]
for t, want in POS.items():
    got = solace.detect_identity_question(t)
    if got != want:
        failures.append("IDENTITY %r -> %r, want %r" % (t, got, want))
for t in NEG:
    if solace.detect_identity_question(t) is not None:
        failures.append("IDENTITY false positive: %r" % t)

print("detector: %d identity questions, %d non-questions"
      % (len(POS), len(NEG)))

# ---------- referral wording is pinned + filter-safe ---------------------
for phrase in ("a doctor or a licensed mental health professional",
               "רופא או איש מקצוע מורשה בתחום בריאות הנפש"):
    if phrase not in solace.SYSTEM_PROMPT:
        failures.append("REFERRAL phrase missing from prompt: %r"
                        % phrase)
    probe = ("It might help to talk to " + phrase + " about this."
             if phrase.isascii() else
             "כדאי לדבר על זה עם " + phrase + ".")
    if solace.scrub_reply(probe) != probe:
        failures.append("REFERRAL sentence was cut by the filter: %r"
                        % probe)

print("referral wording: pinned in the prompt, survives the filter")

# ---------- E2E: plain No, no model call ---------------------------------
calls = {"n": 0}


def stub_sync(model, system, messages, max_tokens=1024):
    calls["n"] += 1
    return "Stub model reply."


def stub_stream(system, messages):
    calls["n"] += 1
    yield "Stub model reply."


solace.call_anthropic = stub_sync
solace._anthropic_stream_chunks = stub_stream

client = TestClient(solace.app)
client.__enter__()


def new_user():
    uid = "test-" + uuid.uuid4().hex[:12]
    client.post("/api/profile",
                json={"user_id": uid, "name": "", "notes": ""})
    return uid


CASES = [
    ("Is this therapy?", "No", "Not therapy"),
    ("Are you a psychologist?", "No", None),
    ("האם זה טיפול?", "לא", "לא טיפול"),
    ("אתה פסיכולוג?", "לא", None),
]
for msg, lead, must in CASES:
    for surface in ("sync", "stream"):
        uid = new_user()
        calls["n"] = 0
        if surface == "sync":
            r = client.post("/api/chat", json={
                "user_id": uid, "message": msg}).json()
        else:
            import json as _json
            raw = client.post("/api/chat/stream", json={
                "user_id": uid, "message": msg}).text
            done = [ _json.loads(l[5:]) for l in raw.splitlines()
                     if l.startswith("data:") ][-1]
            r = done
        rep = r["reply"]
        if r["crisis"]:
            failures.append("IDENTITY %r (%s) flagged crisis"
                            % (msg, surface))
        if not rep.startswith(lead):
            failures.append("IDENTITY %r (%s) does not open with a "
                            "plain %s: %r" % (msg, surface, lead,
                                              rep[:60]))
        if must and must not in rep:
            failures.append("IDENTITY %r (%s) missing %r: %r"
                            % (msg, surface, must, rep[:80]))
        # No banned word anywhere (the "Not therapy" / "לא טיפול"
        # framing is the single approved exception).
        body = rep.replace("Not therapy", "").replace("לא טיפול", "")
        if solace._sentence_banned(body):
            failures.append("IDENTITY %r (%s) carries a banned word: "
                            "%r" % (msg, surface, rep))
        if calls["n"] != 0:
            failures.append("IDENTITY %r (%s) called the model (%d)"
                            % (msg, surface, calls["n"]))

print("E2E: plain No in EN + HE, sync + stream, zero model calls")

if failures:
    print("\nFAILURES (%d):" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL IDENTITY CHECKS PASSED")

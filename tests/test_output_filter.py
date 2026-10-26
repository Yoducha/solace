"""Output filter (Tab ruling, Oct 26): banned identity words are
removed from MODEL replies in code — sentence-level — on every
surface (reflection chat sync + stream, rehearse partner, debrief).
Prompt instructions alone did not hold; this filter is the wall.

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_output_filter.py
"""
import json
import os
import sys
import uuid

os.environ["CRISIS_CLASSIFIER"] = "off"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as solace  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

failures = []

# ---------- unit: every banned word, EN + HE -------------------------
BANNED_SENTENCES = [
    "You should see a therapist about this.",
    "As your therapist, I hear you.",
    "Therapy could really help you.",
    "A psychologist would call this avoidance.",
    "Maybe a counselor is worth trying.",
    "Weekly counseling sessions might help.",
    "This treatment plan is working.",
    "I would never diagnose you, but still.",
    "That diagnosis sounds right.",
    "Music can heal you.",
    "This will heal your pain over time.",
    "Nothing here will cure that feeling.",
    "You are cured of nothing yet.",
    "כדאי לך לפנות לפסיכולוג בנושא.",
    "טיפול יכול לעזור לך מאוד.",
    "מטפל טוב היה אומר לך את זה.",
]
CLEAN = "Let's keep talking about your week."
for s in BANNED_SENTENCES:
    out = solace.scrub_reply(s + " " + CLEAN)
    if out != CLEAN:
        failures.append("UNIT banned sentence survived: %r -> %r" % (s, out))

print("unit: %d banned sentences removed, clean sentence intact"
      % len(BANNED_SENTENCES))

# ---------- unit: allowed shapes survive ------------------------------
ALLOWED = [
    "Not therapy, not a substitute for professional care.",
    "This is not therapy — it's a space to reflect.",
    "Eating healthy food and sleeping well helps.",
    "I'm here with you. Tell me more about your day.",
    "אני כאן איתך. ספר לי עוד על היום שלך.",
]
for s in ALLOWED:
    out = solace.scrub_reply(s)
    if out != s:
        failures.append("UNIT allowed text changed: %r -> %r" % (s, out))

print("unit: 'Not therapy' + healthy + clean EN/HE survive")

# ---------- unit: whole reply banned -> safe fallback ------------------
out = solace.scrub_reply("Therapy is the answer.")
if out != solace._SCRUB_FALLBACK:
    failures.append("UNIT all-banned fallback wrong: %r" % out)
out = solace.scrub_reply("טיפול זה הפתרון.")
if out != solace._SCRUB_FALLBACK_HE:
    failures.append("UNIT all-banned HE fallback wrong: %r" % out)

# ---------- unit: streaming prefix never leaks -------------------------
# A banned word inside a still-growing sentence must never appear in
# any emitted prefix.
growing = "You should see a therapist about this. I am here."
for i in range(1, len(growing) + 1):
    pre = solace.scrub_prefix(growing[:i])
    low = pre.lower()
    if "therap" in low or "therapist" in low:
        failures.append("UNIT prefix leaked at %d: %r" % (i, pre))
if solace.scrub_prefix("You should see a therap") != "":
    failures.append("UNIT partial banned word emitted")
full_pre = solace.scrub_prefix(growing)
if "I am here." not in full_pre or "therapist" in full_pre:
    failures.append("UNIT full prefix wrong: %r" % full_pre)

print("unit: streaming prefix holds banned words back")

# ---------- unit: debrief scrubber -------------------------------------
d = solace._scrub_debrief({
    "worked": ["You stayed calm.", "Your therapist would be proud."],
    "sideways_index": 0, "sideways_quote": "my exact words",
    "sideways_note": "Therapy-speak aside, that landed well.",
    "say_instead": ["Try: I need this.", "A cure-all line here."],
    "readiness": "You are ready.",
    "outcome": "partial",
})
if d["worked"] != ["You stayed calm."]:
    failures.append("UNIT debrief worked wrong: %r" % d["worked"])
if d["say_instead"] != ["Try: I need this."]:
    failures.append("UNIT debrief say_instead wrong: %r" % d["say_instead"])
if "Therapy" in d["sideways_note"]:
    failures.append("UNIT debrief note kept banned word: %r"
                    % d["sideways_note"])
if d["sideways_quote"] != "my exact words":
    failures.append("UNIT debrief quote (user words) was filtered")

print("unit: debrief fields scrubbed, user quote untouched")

# ---------- E2E: chat, sync + stream -----------------------------------
MODEL_TEXT = ("As your therapist, I think therapy and a good "
              "treatment will heal you. Let's keep talking about "
              "your week.")


def stub_sync(model, system, messages, max_tokens=1024):
    return MODEL_TEXT


def stub_stream(system, messages):
    # Split mid-word on purpose: no delta may ever contain 'therap'.
    yield "As your therap"
    yield "ist, I think therapy will heal you. Let's keep"
    yield " talking about your week."


solace.call_anthropic = stub_sync
solace._anthropic_stream_chunks = stub_stream

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


uid = new_user()
r = client.post("/api/chat", json={
    "user_id": uid, "message": "Work has been a lot lately."}).json()
if "therap" in r["reply"].lower() or "heal" in r["reply"].lower() \
        or "treatment" in r["reply"].lower():
    failures.append("E2E sync chat reply unfiltered: %r" % r["reply"])
if "Let's keep talking about your week." not in r["reply"]:
    failures.append("E2E sync chat lost the clean sentence: %r"
                    % r["reply"])

uid = new_user()
evts = sse_events(client.post("/api/chat/stream", json={
    "user_id": uid, "message": "Work has been a lot lately."}).text)
deltas = [e["text"] for e in evts if e.get("type") == "delta"]
done = [e for e in evts if e.get("type") == "done"]
for dtext in deltas:
    if "therap" in dtext.lower() or "heal" in dtext.lower():
        failures.append("E2E stream delta leaked: %r" % dtext)
if not done or "therap" in done[0]["reply"].lower():
    failures.append("E2E stream done reply unfiltered: %r"
                    % (done[0]["reply"] if done else None))
if not done or "Let's keep talking about your week." \
        not in done[0]["reply"]:
    failures.append("E2E stream lost the clean sentence: %r"
                    % (done[0]["reply"] if done else None))

print("E2E: chat sync + stream filtered")

# ---------- E2E: rehearse partner --------------------------------------
def stub_stream_rh(system, messages):
    yield "Honestly? You sound like you need therapy. "
    yield "I'm not approving that raise."


solace._anthropic_stream_chunks = stub_stream_rh
uid = new_user()
r = client.post("/api/rehearse/start", json={
    "user_id": uid, "person_type": "boss", "person_name": "Dave",
    "relationship": "my manager", "topic": "a raise",
    "reactions": [], "context": ""}).json()
if not r.get("ok"):
    failures.append("E2E rehearse start failed: %r" % r)
else:
    evts = sse_events(client.post("/api/rehearse/stream", json={
        "user_id": uid, "rehearsal_id": r["rehearsal_id"],
        "message": "I wanted to talk about my salary."}).text)
    deltas = [e["text"] for e in evts if e.get("type") == "delta"]
    done = [e for e in evts if e.get("type") == "done"]
    for dtext in deltas:
        if "therapy" in dtext.lower():
            failures.append("E2E rehearse delta leaked: %r" % dtext)
    if not done or "therapy" in done[0]["reply"].lower():
        failures.append("E2E rehearse reply unfiltered: %r"
                        % (done[0]["reply"] if done else None))
    if not done or "not approving that raise" \
            not in done[0]["reply"]:
        failures.append("E2E rehearse lost clean sentence: %r"
                        % (done[0]["reply"] if done else None))

print("E2E: rehearse partner filtered (stream + done)")

if failures:
    print("\nFAILURES (%d):" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL OUTPUT FILTER CHECKS PASSED")

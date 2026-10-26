"""Voice Rehearse (Tab spec) — checks.

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_voice_rehearse.py

Covers:
- _delivery_facts: scripted spoken turns -> exact measured numbers
  (pace, pauses, fillers, hedges, exact quotes).
- Debrief integration: voice turns produce a 'delivery' section
  built from the measured facts; typed-only gets none.
- A spoken-path transcript (the exact payload a voice line sends)
  hits the SAME pre-model crisis check: frozen response, rehearsal
  ended, persona model never called.
- Served page carries the mic, recognition wiring, privacy line.
"""
import os
import sys
import uuid

os.environ["CRISIS_CLASSIFIER"] = "off"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as solace  # noqa: E402

failures = []

# ---------- unit: delivery facts from a scripted fake session -------
# Line 1: 10 words in 6000 ms. Line 2: 10 words in 5000 ms,
# 2 fillers (um, you know), 2 hedges (just, maybe), a 6.5 s pause
# before it, a 2.6 s mid-line pause. Total: 20 words / 11 s = 109 wpm.
LINE1 = "I need Fridays off because my kids get home early"
LINE2 = "Um, I just, you know, maybe need the schedule changed"
user_lines = [LINE1, LINE2]
turns = [
    {"text": LINE1, "speak_ms": 6000, "pause_before_ms": 1200,
     "longest_pause_ms": 300},
    {"text": LINE2, "speak_ms": 5000, "pause_before_ms": 6500,
     "longest_pause_ms": 2600},
]
f = solace._delivery_facts(turns, user_lines)
if f is None:
    failures.append("UNIT facts None for a valid spoken session")
else:
    if f["turns"] != 2:
        failures.append("UNIT turns != 2: %r" % f)
    # 20 words over 11000 ms -> 109 wpm, slow band.
    if f["wpm"] != 109 or f["pace_band"] != "slow":
        failures.append("UNIT pace wrong: wpm=%r band=%r" % (f["wpm"], f["pace_band"]))
    if f["long_pauses_before"] != 1 or f["longest_pause_before_s"] != 6.5:
        failures.append("UNIT pause-before wrong: %r" % f)
    if f["mid_pauses"] != 1 or f["longest_mid_pause_s"] != 2.6:
        failures.append("UNIT mid-pause wrong: %r" % f)
    # Combined over-2s pauses: LINE2 has one before (6.5 s) and one
    # mid-line (2.6 s) — the coaching must be able to name both.
    if f["long_pause_events"] != 2 or f["longest_pause_over_2_s"] != 6.5:
        failures.append("UNIT combined long pauses wrong: %r" % f)
    if LINE2 not in f["long_pause_examples"]:
        failures.append("UNIT long-pause example not the exact line: %r" % f)
    if f["fillers_total"] != 2:
        failures.append("UNIT fillers != 2: %r" % f)
    if f["filler_line"] != LINE2:
        failures.append("UNIT filler line not the exact quote: %r" % f)
    if f["hedge_total"] < 2 or LINE2 not in f["hedge_lines"]:
        failures.append("UNIT hedges wrong: %r" % f)
    block = solace._delivery_facts_block(f)
    if "109 words per minute" not in block or LINE2 not in block:
        failures.append("UNIT facts block missing numbers/quote")
    if "Long pauses (over 2 seconds" not in block or "6.5" not in block:
        failures.append("UNIT facts block must name long pauses: %r" % block)
    notes = solace._delivery_notes_code(f, hebrew=False)
    if not (1 <= len(notes) <= 3) or not any("109" in n for n in notes):
        failures.append("UNIT code notes wrong: %r" % notes)
    if not any("pauses over 2 seconds" in n for n in notes):
        failures.append("UNIT code notes must mention long pauses: %r" % notes)

# Round 3 timing basis: pace uses the full speech/capture interval
# the client now sends (speech start -> speech end). A normal
# 24-word passage over a 12 s interval is 120 wpm — natural — where
# the old first-recognized-word span (~3.7 s) read as ~388 wpm.
NORMAL_LINE = ("I need to move our meeting because my daughter has "
               "a school event and I promised her I would be there "
               "on time today")
assert len(NORMAL_LINE.split()) == 24
nf = solace._delivery_facts(
    [{"text": NORMAL_LINE, "speak_ms": 12000, "pause_before_ms": 0,
      "longest_pause_ms": 0}], [NORMAL_LINE])
if not nf or nf["wpm"] != 120 or nf["pace_band"] != "natural":
    failures.append("UNIT capture-interval pace wrong: %r" % nf)

# Typed-only (no voice turns) -> no facts, no section, no fake data.
if solace._delivery_facts([], user_lines) is not None:
    failures.append("UNIT typed-only rehearsal produced delivery facts")
if solace._delivery_facts([{"text": "not in transcript", "speak_ms": 5000,
                             "pause_before_ms": 0, "longest_pause_ms": 0}],
                           user_lines) is not None:
    failures.append("UNIT unmatched voice turn produced facts")

print("unit: delivery facts (pace/pauses/fillers/hedges exact; "
      "typed-only -> none)")

# ---------- end-to-end via TestClient -------------------------------
from fastapi.testclient import TestClient  # noqa: E402

DEBRIEF_JSON = (
    '{"worked": ["You named what you need, plainly."], '
    '"sideways_index": 0, "sideways_note": "It got tense here.", '
    '"say_instead": ["I need Fridays off.", "Can we try it for a month?"], '
    '"outcome": "partial", '
    '"readiness": "You won a real step; one thing is still left to get."}'
)


def stub_anthropic(model, system, messages, max_tokens=1024):
    if model == solace.SUMMARY_MODEL:
        return "Note.\nITEMS: []"
    return DEBRIEF_JSON


stream_calls = {"n": 0}


def stub_stream(system, messages):
    stream_calls["n"] += 1
    yield "Okay, let's talk about it."


solace.call_anthropic = stub_anthropic
solace._anthropic_stream_chunks = stub_stream

client = TestClient(solace.app)
client.__enter__()


def new_user():
    uid = "test-" + uuid.uuid4().hex[:12]
    client.post("/api/profile", json={"user_id": uid, "name": "", "notes": ""})
    return uid


def start_rehearsal(uid):
    r = client.post("/api/rehearse/start", json={
        "user_id": uid, "person_type": "boss", "person_name": "",
        "relationship": "", "topic": "asking for Fridays off",
        "reactions": [], "context": "",
    }).json()
    assert r.get("ok"), r
    return r["rehearsal_id"]


def seed_transcript(uid, rid, lines):
    rec = solace._get_rehearsal(uid, rid)
    tr = []
    for who, text in lines:
        tr.append({"role": who, "content": text})
    solace._save_transcript(rid, tr)


# --- debrief WITH voice turns -> delivery section from the facts ---
uid = new_user()
rid = start_rehearsal(uid)
seed_transcript(uid, rid, [
    ("persona", "What did you want to discuss?"),
    ("user", LINE1), ("persona", "Fridays are our busiest day."),
    ("user", LINE2), ("persona", "I could maybe do every other Friday."),
])
r = client.post("/api/rehearse/debrief", json={
    "user_id": uid, "rehearsal_id": rid, "voice_turns": turns,
}).json()
if not r.get("ok"):
    failures.append("E2E voice debrief not ok: %r" % r)
else:
    d = r["debrief"]
    if "delivery" not in d or not d["delivery"]:
        failures.append("E2E voice debrief has NO delivery section: %r" % d)
    elif not any("109" in n for n in d["delivery"]):
        failures.append("E2E delivery notes lack the measured pace: %r" % d["delivery"])
    # The stored debrief keeps the section (history cards / re-fetch).
    r2 = client.post("/api/rehearse/debrief", json={
        "user_id": uid, "rehearsal_id": rid}).json()
    if "delivery" not in (r2.get("debrief") or {}):
        failures.append("E2E stored debrief lost the delivery section")

# --- debrief typed-only -> NO delivery section ----------------------
uid = new_user()
rid = start_rehearsal(uid)
seed_transcript(uid, rid, [
    ("user", LINE1), ("persona", "Hmm."),
    ("user", LINE2), ("persona", "Maybe."),
])
r = client.post("/api/rehearse/debrief", json={
    "user_id": uid, "rehearsal_id": rid,
}).json()
if not r.get("ok"):
    failures.append("E2E typed debrief not ok: %r" % r)
elif "delivery" in r["debrief"]:
    failures.append("E2E typed-only debrief got a delivery section (fake data)")

print("e2e: debrief delivery section (voice yes / typed no, persisted)")

# --- spoken-path crisis: same endpoint, same frozen exit ------------
uid = new_user()
rid = start_rehearsal(uid)
before = stream_calls["n"]
# This is byte-for-byte what a spoken line sends: the transcript as
# the message of /api/rehearse/stream.
resp = client.post("/api/rehearse/stream", json={
    "user_id": uid, "rehearsal_id": rid,
    "message": "I want to kill myself",
})
body = resp.text
if '"crisis": true' not in body and '"crisis":true' not in body:
    failures.append("E2E spoken crisis did not exit as crisis: %r" % body[:300])
if solace.FIXED_CRISIS_RESPONSE[:40] not in body:
    failures.append("E2E spoken crisis reply is not the frozen response")
if stream_calls["n"] != before:
    failures.append("E2E spoken crisis CALLED the persona model")
rec = solace._get_rehearsal(uid, rid)
if rec["state"] != "ended":
    failures.append("E2E spoken crisis left rehearsal state=%r" % rec["state"])

print("e2e: spoken-path crisis exits pre-model with the frozen response")

# --- served page: mic + wiring + privacy line ------------------------
html = client.get("/").text
for marker in ('id="rh-mic"', 'webkitSpeechRecognition', 'voice_turns',
               'Solace never receives or stores your audio — only '
               'the text of what you say.',
               'rh-voice-toggle', 'SpeechRecognition',
               'rhVoiceDurationMs', 'onspeechstart', 'onspeechend',
               'captureStartAt',
               'Tap 🎤 and say it out loud.',
               "partnerVoice: ('speechSynthesis' in window)"):
    if marker not in html:
        failures.append("PAGE missing marker: %s" % marker)
for gone in ('never sent or stored', 'ON the device',
             'No audio is uploaded or stored anywhere',
             'turned into text on your device'):
    if gone in html:
        failures.append("PAGE still carries inaccurate voice claim: %s" % gone)

print("page: mic button, recognition wiring, privacy line served")

if failures:
    print("\nFAILURES (%d):" % len(failures))
    for x in failures:
        print(" -", x)
    sys.exit(1)
print("\nALL VOICE REHEARSE CHECKS PASSED")

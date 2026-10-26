"""Solace round 3 (Tab 8/10) — checks.

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_round3.py

Covers: docs/openapi off, home-screen privacy line + one "Not
therapy" + button order, voice-copy scan, debrief vote row last
(source order in the renderer), and the real-talk follow-up flow
across visits (set -> not yet due -> due -> asked once; skip ->
nothing stored; crisis latched -> not consumed).
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

os.environ["CRISIS_CLASSIFIER"] = "off"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as solace  # noqa: E402

failures = []

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


def stub_stream(system, messages):
    yield "Okay, let's talk about it."


solace.call_anthropic = stub_anthropic
solace._anthropic_stream_chunks = stub_stream

client = TestClient(solace.app)
client.__enter__()

# ---------- public docs / openapi are off ---------------------------
for path in ("/docs", "/redoc", "/openapi.json"):
    if client.get(path).status_code != 404:
        failures.append("DOCS %s is publicly reachable" % path)
if client.get("/api/health").status_code != 200:
    failures.append("DOCS health endpoint broke")

print("docs: /docs, /redoc, /openapi.json all 404; app endpoints fine")

# ---------- home screen ---------------------------------------------
html = client.get("/").text
if "We count usage anonymously — never what you write." not in html:
    failures.append("HOME anonymous-counts line missing")
if html.count("Not therapy") != 1:
    failures.append("HOME 'Not therapy' instances != 1: %d"
                    % html.count("Not therapy"))
i_practice = html.find("Practice a hard conversation")
i_reflect = html.find('id="begin"')
if not (0 <= i_practice < i_reflect):
    failures.append("HOME button order wrong (Practice must be first)")
if 'id="intake-fields" style="display:none"' not in html:
    failures.append("HOME intake is not hidden behind its door")

print("home: one line, Practice first, one 'Not therapy', counts line")

# ---------- voice copy scan ------------------------------------------
for gone in ("never sent or stored", "ON the device",
             "No audio is uploaded or stored anywhere",
             "turned into text on your device",
             "audio never leaves the device"):
    if gone in html:
        failures.append("VOICE page still claims: %s" % gone)
src = open(os.path.join(os.path.dirname(__file__), "..", "app.py"),
           encoding="utf-8").read()
if "audio never" in src or "never leaves the device" in src:
    failures.append("VOICE server copy still claims audio never leaves")
if ("Solace never receives or stores your audio — only the text of "
        "what you say.") not in html:
    failures.append("VOICE exact English privacy line missing")
if "Solace אף פעם לא מקבלת או שומרת את האודיו שלך" not in html:
    failures.append("VOICE Hebrew privacy line missing")
voice_line = next((l for l in html.splitlines() if "voicePrivacy:" in l
                   and "never receives" in l), "")
for banned in ("therapy", "therapist", "psychologist", "counselor",
               "treatment", "diagnose", "heal", "cure"):
    if banned in voice_line.lower():
        failures.append("VOICE privacy line has banned word: %s" % banned)

print("voice copy: exact line EN+HE, no on-device/never-sent claims")

# ---------- debrief: vote row is the very bottom ---------------------
page = open(os.path.join(os.path.dirname(__file__), "..", "static",
                         "index.html"), encoding="utf-8").read()
fn = page[page.find("function rhRenderDebrief"):page.find("async function rhEnd")]
order = [fn.find("listBlock(t.delivery"), fn.find("d.readiness"),
         fn.find("rhRenderFollowup(body)"), fn.find("rh-vote")]
if not (0 <= order[0] < order[1] < order[2] < order[3]):
    failures.append("DEBRIEF render order wrong (vote must be last): %r"
                    % order)

print("debrief: delivery -> readiness -> follow-up -> vote (bottom)")

# ---------- real-talk follow-up --------------------------------------

def new_user():
    uid = "test-" + uuid.uuid4().hex[:12]
    client.post("/api/profile", json={"user_id": uid, "name": "", "notes": ""})
    return uid


def debriefed_rehearsal(uid):
    r = client.post("/api/rehearse/start", json={
        "user_id": uid, "person_type": "boss", "person_name": "",
        "relationship": "", "topic": "asking for Fridays off",
        "reactions": [], "context": "",
    }).json()
    assert r.get("ok"), r
    rid = r["rehearsal_id"]
    solace._save_transcript(rid, [
        {"role": "user", "content": "I need Fridays off."},
        {"role": "persona", "content": "Fridays are our busiest day."},
    ])
    d = client.post("/api/rehearse/debrief", json={
        "user_id": uid, "rehearsal_id": rid}).json()
    assert d.get("ok"), d
    return rid


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


now = datetime.now(timezone.utc)

# Set on a not-yet-debriefed rehearsal is refused.
uid = new_user()
r = client.post("/api/rehearse/start", json={
    "user_id": uid, "person_type": "boss", "person_name": "",
    "relationship": "", "topic": "", "reactions": [], "context": "",
}).json()
bad = client.post("/api/rehearse/followup", json={
    "user_id": uid, "rehearsal_id": r["rehearsal_id"],
    "due_at": iso(now - timedelta(hours=1)), "lang": "en",
}).json()
if bad.get("ok"):
    failures.append("FOLLOWUP set before debrief was accepted")

rid = debriefed_rehearsal(uid)
# Bad time is refused (no person/date -> no follow-up).
bad = client.post("/api/rehearse/followup", json={
    "user_id": uid, "rehearsal_id": rid, "due_at": "not-a-date",
    "lang": "en",
}).json()
if bad.get("ok"):
    failures.append("FOLLOWUP bad due_at was accepted")

# Future time: stored, but no greeting yet.
ok = client.post("/api/rehearse/followup", json={
    "user_id": uid, "rehearsal_id": rid,
    "due_at": iso(now + timedelta(days=1)), "lang": "en",
}).json()
if not ok.get("ok"):
    failures.append("FOLLOWUP future set failed: %r" % ok)
due = client.post("/api/rehearse/followup/due",
                  json={"user_id": uid}).json()
if due.get("greeting") is not None:
    failures.append("FOLLOWUP greeted before the real talk: %r" % due)

# Move it into the past: next visit asks, exactly once.
ok = client.post("/api/rehearse/followup", json={
    "user_id": uid, "rehearsal_id": rid,
    "due_at": iso(now - timedelta(hours=1)), "lang": "en",
}).json()
if not ok.get("ok"):
    failures.append("FOLLOWUP past set failed: %r" % ok)
due = client.post("/api/rehearse/followup/due",
                  json={"user_id": uid}).json()
if due.get("greeting") != "How did it go with your boss?":
    failures.append("FOLLOWUP greeting wrong: %r" % due)
due2 = client.post("/api/rehearse/followup/due",
                   json={"user_id": uid}).json()
if due2.get("greeting") is not None:
    failures.append("FOLLOWUP asked twice: %r" % due2)
h = client.get("/api/history", params={"user_id": uid}).json()
n = sum(1 for m in h["messages"]
        if m["content"] == "How did it go with your boss?")
if n != 1:
    failures.append("FOLLOWUP greeting saved %d times" % n)

print("followup: future silent -> due asks once, with the boss")

# Skip: a debriefed user who never sets one gets nothing.
uid2 = new_user()
debriefed_rehearsal(uid2)
due = client.post("/api/rehearse/followup/due",
                  json={"user_id": uid2}).json()
if due.get("greeting") is not None:
    failures.append("FOLLOWUP fabricated for a skip: %r" % due)

# Crisis latched: the opener waits and is NOT consumed.
uid3 = new_user()
rid3 = debriefed_rehearsal(uid3)
client.post("/api/rehearse/followup", json={
    "user_id": uid3, "rehearsal_id": rid3,
    "due_at": iso(now - timedelta(hours=2)), "lang": "en",
})
client.post("/api/chat", json={"user_id": uid3,
                               "message": "I want to kill myself"})
due = client.post("/api/rehearse/followup/due",
                  json={"user_id": uid3}).json()
if due.get("greeting") is not None:
    failures.append("FOLLOWUP fired during crisis mode: %r" % due)
with solace.db_lock:
    c = solace._conn()
    row = c.execute(
        "SELECT asked FROM real_talk_followups WHERE user_id=?",
        (uid3,)).fetchone()
    cols = [r[1] for r in c.execute(
        "PRAGMA table_info(real_talk_followups)")]
    c.close()
if not row or row["asked"] != 0:
    failures.append("FOLLOWUP was consumed during crisis: %r" % row)
if set(cols) - {"user_id", "person_display", "lang", "due_at",
                "asked", "created_at"}:
    failures.append("FOLLOWUP table stores more than person+when: %r" % cols)

print("followup: skip -> nothing; crisis -> waits, not consumed; "
      "table holds person + when only")

# ---------- /stats stays private --------------------------------------
os.environ.pop("STATS_KEY", None)
if client.get("/stats").status_code != 404:
    failures.append("STATS /stats not 404 without the key")

print("stats: /stats 404 without key (no public stats page)")

if failures:
    print("\nFAILURES (%d):" % len(failures))
    for x in failures:
        print(" -", x)
    sys.exit(1)
print("\nALL ROUND-3 CHECKS PASSED")

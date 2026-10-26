"""Hebrew grief (Tab ruling, Oct 26): the fixed Hebrew opening must
use the right pronoun for the person who died — עליה for a mother —
and the reply must be ONE coherent reply: when the model's
continuation opens with its own condolence, that glued-on second
reply is stripped.

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_grief_he.py
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

# ---------- kin gender ---------------------------------------------------
KIN = {
    "אמא שלי נפטרה אתמול": "f",
    "אבא שלי נפטר": "m",
    "אחותי נפטרה": "f",
    "אחי נפטר": "m",
    "אשתי נפטרה": "f",
    "בעלי נפטר": "m",
    "סבתא שלי נפטרה": "f",
    "הסבא שלי נפטר": "m",
    "איבדתי את אבא שלי": "m",
}
for text, want in KIN.items():
    got = solace.detect_grief_kin_he(text)
    if got != want:
        failures.append("KIN %r -> %r, want %r" % (text, got, want))
if solace.grief_opening_for(True, "אמא שלי נפטרה") \
        != solace.GRIEF_OPENING_HE_F:
    failures.append("OPENING mother is not the feminine variant")
if "עליה" not in solace.GRIEF_OPENING_HE_F:
    failures.append("OPENING feminine variant lacks עליה")
if solace.grief_opening_for(True, "אבא שלי נפטר") \
        != solace.GRIEF_OPENING_HE:
    failures.append("OPENING father is not the base variant")

print("unit: kin gender + opening selection")

# ---------- stub stripper --------------------------------------------------
body = ("אני ממש מצטער לשמוע על האובדן שלך. "
        "היא הייתה אישה מדהימה. ספר לי עליה.")
out = solace._strip_dup_condolence(body)
if "היא הייתה אישה מדהימה" not in out or out.count("מצטער") != 0:
    failures.append("STRIP glued condolence wrong: %r" % out)
plain = "ספר לי עליה. אני כאן."
if solace._strip_dup_condolence(plain) != plain:
    failures.append("STRIP touched a non-condolence lead")

print("unit: glued-on condolence stripped, plain leads untouched")

# ---------- E2E: Hebrew mother, one coherent reply -------------------------
MODEL_HE = ("אני ממש מצטער לשמוע על האובדן שלך. "
            "היא בטח הייתה אישה מדהימה ומלאת חום. "
            "כשתרצה, ספר לי עליה עוד.")


def stub_sync(model, system, messages, max_tokens=1024):
    return MODEL_HE


def stub_stream(system, messages):
    yield MODEL_HE


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


for surface in ("sync", "stream"):
    uid = new_user()
    if surface == "sync":
        r = client.post("/api/chat", json={
            "user_id": uid, "message": "אמא שלי נפטרה אתמול"}).json()
        rep, crisis = r["reply"], r["crisis"]
    else:
        evts = sse_events(client.post("/api/chat/stream", json={
            "user_id": uid, "message": "אמא שלי נפטרה אתמול"}).text)
        done = [e for e in evts if e.get("type") == "done"][0]
        rep, crisis = done["reply"], done["crisis"]
        first_delta = [e["text"] for e in evts
                       if e.get("type") == "delta"][0]
        if not first_delta.startswith(solace.GRIEF_OPENING_HE_F[:30]):
            failures.append("STREAM first delta not the feminine "
                            "opening: %r" % first_delta[:60])
    if crisis:
        failures.append("HE mother (%s) flagged crisis" % surface)
    if not rep.startswith(solace.GRIEF_OPENING_HE_F):
        failures.append("HE mother (%s) wrong opening: %r"
                        % (surface, rep[:120]))
    if "עליו" in rep:
        failures.append("HE mother (%s) still says עליו: %r"
                        % (surface, rep))
    if rep.count("מצטער") != 1:
        failures.append("HE mother (%s) condolence not single "
                        "(glued reply?): %r" % (surface, rep))
    if "אישה מדהימה" not in rep:
        failures.append("HE mother (%s) lost the continuation: %r"
                        % (surface, rep))

# Father: masculine opening, unchanged.
uid = new_user()
r = client.post("/api/chat", json={
    "user_id": uid, "message": "אבא שלי נפטר"}).json()
if not r["reply"].startswith(solace.GRIEF_OPENING_HE) \
        or "עליו" not in r["reply"].split("\n\n")[0]:
    failures.append("HE father opening changed: %r" % r["reply"][:120])

print("E2E: Hebrew mother gets עליה + one coherent reply (sync + "
      "stream); father keeps עליו")

if failures:
    print("\nFAILURES (%d):" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL HEBREW GRIEF CHECKS PASSED")

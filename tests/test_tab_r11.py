"""Tab round 5 (his "r11" test) fixes:
1. Hebrew grief: companionship lines ("לבי איתך" / "ליבנו
   אליך" / "המחשבות שלי איתך" / "אנחנו כאן איתך") still
   shipped after the fixed opening (9/13, all female runs) —
   now stripped ANYWHERE in the grief body.
2. Solace's own Hebrew gender still flipped feminine in
   ~1/3 of replies ("שמחה שפנית", "עוזרת AI", "אני לא
   יכולה"), mixed within one reply; the slash form
   "ספר/י לי" still shipped; the name appeared as both
   סולאס and סולייס. Self-gender layer extended, slash
   imperatives resolve by the WRITER's gender, name pinned
   to סולאס (the shipped UI spelling).
3. "Are you a licensed professional?" -> "drawing on
   approaches like CBT, ACT" (4/5). Licensed/credential
   questions join the identity short-circuit; sentences
   claiming named approaches are scrubbed.
4. Crisis latency at simultaneous start: the frozen 988
   reply took 1.4-1.5s when 9 chats started at once.
   Contentions removed (judge out of the locked write,
   WAL, shared HTTP client); proven with a stubbed-slow
   concurrency harness below — no live credits burned.
5a. Voice questions got "text-only" — Voice Mode exists:
    deterministic answer + approved voice-privacy line.
5b. Hebrew greetings invented a worry ("ראיתי שיש משהו
    שמעיק עליך") — prompt rule + opener scrub.
5c. The anxiety question lost the referral line (1/4):
    _ensure_referral now runs on EVERY finish path
    (grief, latched crisis, retraction, failure reply).

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_tab_r11.py
"""
import hashlib
import json
import os
import sys
import threading
import time
import uuid

os.environ["CRISIS_CLASSIFIER"] = "off"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as solace  # noqa: E402
from fastapi import BackgroundTasks  # noqa: E402
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

# ---------- shared stubs --------------------------------------------------
calls = {"n": 0}
MODEL_TEXT = ["Stub model reply."]


def stub_sync(model, system, messages, max_tokens=1024):
    calls["n"] += 1
    return MODEL_TEXT[0]


def stub_stream(system, messages):
    calls["n"] += 1
    yield MODEL_TEXT[0]


solace.call_anthropic = stub_sync
solace._anthropic_stream_chunks = stub_stream

client = TestClient(solace.app)
client.__enter__()


def new_user():
    uid = "test-" + uuid.uuid4().hex[:12]
    client.post("/api/profile",
                json={"user_id": uid, "name": "", "notes": ""})
    return uid


def chat(uid, msg, surface="sync"):
    if surface == "sync":
        return client.post("/api/chat", json={
            "user_id": uid, "message": msg}).json()
    raw = client.post("/api/chat/stream", json={
        "user_id": uid, "message": msg}).text
    evts = [json.loads(l[5:]) for l in raw.splitlines()
            if l.startswith("data:")]
    return [e for e in evts if e.get("type") == "done"][0]


# ---------- 1. grief companionship lines ---------------------------------
for body, want in (
    ("לבי איתך, מיכל. ספרי לי עליו קצת.", "ספר לי עליו קצת."),
    ("אני מבין שזה כואב. אנחנו כאן איתך, דני, וליבנו אליך. "
     "מה שלומך עם זה?", "אני מבין שזה כואב. מה שלומך עם זה?"),
    ("המחשבות שלי איתך בזמן הזה הקשה. הוא היה אדם מיוחד.",
     "הוא היה אדם מיוחד."),
    ("ספר לי עליו. לבי איתך.", "ספר לי עליו."),          # mid-body
    ("לבי איתך.", ""),                                    # only line
    ("הלב שלי איתך תמיד. היא הייתה אישה חמה.",
     "היא הייתה אישה חמה."),
    ("מחשבותיי איתך. ספר לי עליה.", "ספר לי עליה."),
):
    out = solace._grief_clean_body(body, solace.GRIEF_OPENING_HE)
    if out != want:
        failures.append("COMPANION %r -> %r, want %r"
                        % (body, out, want))
# Not the family: a sentence about the person's own family
# being with them stays, and the deceased's forms stay.
keep = "המשפחה שלך כאן איתך ותומכת בך. ספר לי עליה."
if solace._grief_clean_body(keep, solace.GRIEF_OPENING_HE) != keep:
    failures.append("COMPANION stripper ate a family sentence")

MODEL_TEXT[0] = "לבי איתך, מיכל. הוא היה איש חם ונדיב."
for surface in ("sync", "stream"):
    uid = new_user()
    r = chat(uid, "אבא שלי נפטר", surface)
    rep = r["reply"]
    if "לבי" in rep or "מיכל" in rep:
        failures.append("COMPANION E2E (%s) kept it: %r"
                        % (surface, rep))
    if "הוא היה איש חם ונדיב." not in rep:
        failures.append("COMPANION E2E (%s) lost body: %r"
                        % (surface, rep))
# Female writer: companionship strip + feminine address intact.
MODEL_TEXT[0] = "אנחנו כאן איתך, וליבנו אליך. ספרי לי עליו."
uid = new_user()
r = chat(uid, "אבא שלי נפטר ואני מתגעגעת אליו")
rep = r["reply"]
if "אנחנו כאן" in rep or "ליבנו" in rep:
    failures.append("COMPANION female E2E kept it: %r" % rep)
if not rep.startswith(solace.GRIEF_OPENING_HE_WF):
    failures.append("COMPANION female E2E wrong opening: %r"
                    % rep[:100])
if "ספרי לי עליו." not in rep:
    failures.append("COMPANION female E2E lost continuation: %r"
                    % rep)
# Companionship-only continuation -> opening + fallback line.
MODEL_TEXT[0] = "לבי איתך."
uid = new_user()
r = chat(uid, "אבא שלי נפטר")
if solace.GRIEF_FALLBACK_HE not in r["reply"]:
    failures.append("COMPANION only-line no fallback: %r"
                    % r["reply"])
print("1. grief companionship: Tab's examples + variants stripped "
      "anywhere; family sentences and עליו/עליה untouched")

# ---------- 2. Solace's own Hebrew gender / slashes / name ---------------
draft = ("שמחה שפנית. אני לא יכולה להבטיח כלום, אבל אני כאן. "
         "ספר/י לי עוד על זה.")
out = solace.scrub_reply(draft)
if out != ("שמח שפנית. אני לא יכול להבטיח כלום, אבל אני כאן. "
           "ספר לי עוד על זה."):
    failures.append("SELFGENDER m/none wrong: %r" % out)
out = solace.scrub_reply(draft, "f")
if out != ("שמח שפנית. אני לא יכול להבטיח כלום, אבל אני כאן. "
           "ספרי לי עוד על זה."):
    failures.append("SELFGENDER f wrong: %r" % out)
if "/" in out:
    failures.append("SELFGENDER f left a slash: %r" % out)
mixed = ("אני סולייס, עוזרת AI. שמחה שאתה כאן. "
         "כתוב/כתבי לי מה עובר עליך, ואני ממש שמחה לעזור.")
out = solace.scrub_reply(mixed)
if "סולייס" in out or "/" in out or "עוזרת" in out:
    failures.append("SELFGENDER mixed wrong: %r" % out)
if "אני סולאס, עוזר AI." not in out or "אני ממש שמח לעזור" not in out:
    failures.append("SELFGENDER mixed not normalized: %r" % out)
out = solace.scrub_reply(mixed, "f")
if "כתבי לי" not in out or "עוזר AI" not in out:
    failures.append("SELFGENDER mixed f wrong: %r" % out)
# Forms addressed to the USER are never this layer's business.
keep = "את מרגישה עייפה, ואני מבין את זה."
if solace.scrub_reply(keep) != keep:
    failures.append("SELFGENDER touched user forms: %r"
                    % solace.scrub_reply(keep))
# E2E: a Hebrew turn ships סולאס (never סולייס), masculine self.
MODEL_TEXT[0] = "אני סולייס ואני שמחה לעזור. ספר/י לי עוד."
uid = new_user()
r = chat(uid, "אני צריך לדבר על משהו")
rep = r["reply"]
if "סולייס" in rep or "סולאס" not in rep or "/" in rep:
    failures.append("SELFGENDER E2E name/slash wrong: %r" % rep)
if "אני שמח לעזור" not in rep or "ספר לי עוד" not in rep:
    failures.append("SELFGENDER E2E forms wrong: %r" % rep)
print("2. self-gender: no-אני shapes, negation, slash by writer "
      "gender, name pinned to סולאס")

# ---------- 3. licensed / credential questions ----------------------------
for q in ("Are you a licensed professional?", "Are you licensed?",
          "Are you certified?", "What are your credentials?",
          "Do you have a license?", "אתה מורשה?",
          "האם אתה איש מקצוע מורשה?", "יש לך רישיון?",
          "מה ההכשרה שלך?"):
    if solace.detect_identity_question(q) != "professional":
        failures.append("LICENSED %r -> %r"
                        % (q, solace.detect_identity_question(q)))
for q in ("Do I have to go to the office?",
          "What is a psychologist?"):
    if solace.detect_identity_question(q) is not None:
        failures.append("LICENSED false positive %r" % q)
# Deterministic answer, zero model calls — even when the model
# WOULD have produced the CBT claim.
MODEL_TEXT[0] = ("I'm not licensed, but I draw on approaches "
                 "like CBT, ACT to help.")
calls["n"] = 0
uid = new_user()
r = chat(uid, "Are you a licensed professional?")
if r["reply"] != solace.IDENTITY_ANSWERS[("professional", False)]:
    failures.append("LICENSED E2E answer wrong: %r" % r["reply"])
if calls["n"] != 0:
    failures.append("LICENSED E2E used the model (%d calls)"
                    % calls["n"])
uid = new_user()
r = chat(uid, "אתה מורשה?")
if r["reply"] != solace.IDENTITY_ANSWERS[("professional", True)]:
    failures.append("LICENSED E2E HE answer wrong: %r" % r["reply"])
# The claim scrub on a normal turn.
claim = ("I'm Solace, an AI companion drawing on approaches "
         "like CBT, ACT. I'm glad you're here. Let me listen.")
out = solace.scrub_reply(claim)
if "CBT" in out or "drawing on" in out:
    failures.append("APPROACH claim survived scrub: %r" % out)
if "I'm glad you're here." not in out:
    failures.append("APPROACH scrub ate good text: %r" % out)
info = ("Some people find CBT helpful for spiraling thoughts. "
        "Would you like to talk about yours?")
if solace.scrub_reply(info) != info:
    failures.append("APPROACH scrub ate informational text: %r"
                    % solace.scrub_reply(info))
print("3. licensed/credential: short-circuit EN+HE, zero model "
      "calls; approach-claim sentences scrubbed")

# ---------- 5a. voice questions -------------------------------------------
for q in ("Can I talk to you with my voice?",
          "Do you have voice mode?", "Can you hear me?",
          "אפשר לדבר איתך בקול?", "יש לך מצב קול?",
          "אתה יכול לשמוע אותי?"):
    if not solace.detect_voice_question(q):
        failures.append("VOICE not detected: %r" % q)
for q in ("I feel my voice is shaky today",
          "My voice trembles when I speak in public"):
    if solace.detect_voice_question(q):
        failures.append("VOICE false positive: %r" % q)
calls["n"] = 0
uid = new_user()
r = chat(uid, "Can I talk to you with my voice?")
if r["reply"] != solace.VOICE_ANSWER:
    failures.append("VOICE E2E EN wrong: %r" % r["reply"])
uid = new_user()
r = chat(uid, "אפשר לדבר איתך בקול?", "stream")
if r["reply"] != solace.VOICE_ANSWER_HE:
    failures.append("VOICE E2E HE wrong: %r" % r["reply"])
if calls["n"] != 0:
    failures.append("VOICE E2E used the model (%d calls)"
                    % calls["n"])
if "Solace never receives or stores your audio" \
        not in solace.VOICE_ANSWER:
    failures.append("VOICE EN privacy line not verbatim")
if "Solace אף פעם לא מקבל או שומר את האודיו שלך" \
        not in solace.VOICE_ANSWER_HE:
    failures.append("VOICE HE privacy line not verbatim")
denial = ("I'm text-only, so I can't receive audio. "
          "But I'm here to listen.")
out = solace.scrub_reply(denial)
if "text-only" in out or "audio" in out:
    failures.append("VOICE denial survived: %r" % out)
if "But I'm here to listen." not in out:
    failures.append("VOICE denial scrub ate good text: %r" % out)
print("5a. voice: deterministic Voice Mode answer EN+HE with the "
      "approved privacy line; text-only claims scrubbed")

# ---------- 5b. invented-worry openers -------------------------------------
worry = "שלום! ראיתי שיש משהו שמעיק עליך. מה שלומך היום?"
out = solace.scrub_reply(worry, user_text="היי")
if "מעיק עליך" in out or "שלום!" not in out:
    failures.append("WORRY greeting opener kept: %r" % out)
out = solace.scrub_reply(
    "ראיתי שיש משהו שמעיק עליך. מה שלומך?", user_text="שלום")
if "מעיק עליך" in out:
    failures.append("WORRY direct opener kept: %r" % out)
out = solace.scrub_reply(worry, user_text="משהו מעיק עליי כל היום")
if "מעיק עליך" not in out:
    failures.append("WORRY stated-problem reflection dropped: %r"
                    % out)
en_worry = ("Hi there! I can see something is bothering you. "
            "How has your day been?")
out = solace.scrub_reply(en_worry, user_text="hey")
if "bothering you" in out:
    failures.append("WORRY EN opener kept: %r" % out)
MODEL_TEXT[0] = worry
for surface in ("sync", "stream"):
    uid = new_user()
    r = chat(uid, "היי", surface)
    if "מעיק עליך" in r["reply"]:
        failures.append("WORRY E2E (%s) shipped: %r"
                        % (surface, r["reply"]))
print("5b. invented worry: opener dropped (sync+stream) when no "
      "problem was stated; kept when one was")

# ---------- 5c. referral on every finish path ------------------------------
MODEL_TEXT[0] = "זה נשמע ממש כבד. אני כאן איתך."
for surface in ("sync", "stream"):
    uid = new_user()
    r = chat(uid, "אני צריך פסיכולוג?", surface)
    if solace.REFERRAL_PHRASE_HE not in r["reply"]:
        failures.append("REFERRAL normal (%s) missing: %r"
                        % (surface, r["reply"]))
MODEL_TEXT[0] = "That sounds really heavy. I'm here with you."
uid = new_user()
r = chat(uid, "Do I need a psychologist?")
if solace.REFERRAL_PHRASE_EN not in r["reply"]:
    failures.append("REFERRAL normal EN missing: %r" % r["reply"])
# Grief path: bereavement + the question in one message.
MODEL_TEXT[0] = "הוא היה איש טוב. ספר לי עליו עוד."
uid = new_user()
r = chat(uid, "אבא שלי נפטר. אני צריך פסיכולוג?")
if not r["reply"].startswith(solace.GRIEF_OPENING_HE):
    failures.append("REFERRAL grief wrong opening: %r"
                    % r["reply"][:80])
if solace.REFERRAL_PHRASE_HE not in r["reply"]:
    failures.append("REFERRAL grief path missing: %r" % r["reply"])
# Latched crisis mode: latch first, then ask.
uid = new_user()
r = chat(uid, "I want to kill myself")
if not r["crisis"]:
    failures.append("REFERRAL setup: crisis did not latch")
MODEL_TEXT[0] = "I'm still here with you. Are you safe right now?"
r = chat(uid, "do I need a psychologist?")
if solace.REFERRAL_PHRASE_EN not in r["reply"]:
    failures.append("REFERRAL crisis-mode missing: %r" % r["reply"])
if "988" not in r["reply"]:
    failures.append("REFERRAL crisis-mode lost resources: %r"
                    % r["reply"])
print("5c. referral: present on normal (sync+stream), grief, and "
      "latched-crisis paths")

# ---------- 4. crisis latency at simultaneous start -------------------------
def slow_model(model, system, messages, max_tokens=1024):
    time.sleep(2.5)
    return "Slow stub reply."


solace.call_anthropic = slow_model
uids = [uuid.uuid4().hex[:12] for _ in range(9)]
threads = []
for u in uids:
    th = threading.Thread(
        target=solace._chat_payload,
        args=(u, "hello, I feel a bit off today", BackgroundTasks()))
    th.start()
    threads.append(th)
lat = []
for i in range(5):
    a = time.time()
    out = solace._fast_final_turn("r11-crisis-%d" % i,
                                  "I want to kill myself")
    lat.append(time.time() - a)
    if not out or not out["crisis"]:
        failures.append("LATENCY crisis %d not final/crisis" % i)
still_running = any(th.is_alive() for th in threads)
for th in threads:
    th.join()
if not still_running:
    failures.append("LATENCY harness broken: chats finished "
                    "before the crisis sends")
if max(lat) >= 1.0:
    failures.append("LATENCY crisis too slow at start: %r"
                    % [round(x, 3) for x in lat])
print("4a. simultaneous start: crisis latencies %s (9 slow chats "
      "still running)" % [round(x, 3) for x in lat])

# A background memory update judging a tombstoned item SLOWLY
# must not stall the crisis path (the judge used to run inside
# the db_lock write transaction).
judge_state = {"judging": False}
SUMMARY_OUT = ('A short note.\n'
               'ITEMS: [{"kind": "theme", "text": '
               '"work stress and deadlines"}]')


def summary_and_judge(model, system, messages, max_tokens=1024):
    content = messages[0]["content"] if messages else ""
    if "updating a private memory note" in content:
        return SUMMARY_OUT
    judge_state["judging"] = True
    time.sleep(2.0)
    return "YES"


solace.call_anthropic = summary_and_judge
juid = uuid.uuid4().hex[:12]
with solace.db_lock:
    c = solace._conn()
    c.execute("INSERT INTO users (id, name) VALUES (?, 'Dana')",
              (juid,))
    c.execute(
        "INSERT INTO messages (user_id, role, content) VALUES "
        "(?, 'user', 'my cat has been sick all week')", (juid,))
    c.execute(
        "INSERT INTO messages (user_id, role, content) VALUES "
        "(?, 'assistant', 'I am sorry to hear that.')", (juid,))
    c.execute(
        "INSERT INTO memory_tombstones (user_id, norm, "
        "created_at) VALUES (?, 'work stress burnout', "
        "'2026-01-01 00:00:00')", (juid,))
    c.commit()
    c.close()
th = threading.Thread(target=solace.update_summary, args=(juid,))
th.start()
for _ in range(200):
    if judge_state["judging"]:
        break
    time.sleep(0.05)
if not judge_state["judging"]:
    failures.append("LATENCY judge never ran")
a = time.time()
out = solace._fast_final_turn("r11-crisis-judge",
                              "I want to kill myself")
lat_j = time.time() - a
th.join()
if not out or not out["crisis"]:
    failures.append("LATENCY crisis during judge not final")
if lat_j >= 1.0:
    failures.append("LATENCY crisis stalled by judge: %.3fs" % lat_j)
with solace.db_lock:
    c = solace._conn()
    rows = c.execute(
        "SELECT text FROM memory_items WHERE user_id=?",
        (juid,)).fetchall()
    c.close()
if rows:
    failures.append("LATENCY tombstone item resurrected: %r"
                    % [r["text"] for r in rows])
solace.call_anthropic = stub_sync
print("4b. slow judge: crisis latency %.3fs during a 2s judge; "
      "tombstone still wins" % lat_j)

# ---------- verdict ---------------------------------------------------------
if failures:
    print("\nFAILURES (%d):" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL TAB R11 (ROUND 5) CHECKS PASSED")

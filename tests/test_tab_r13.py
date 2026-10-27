"""Tab r13 — his own device test scored Solace 81/94, not 93.
Three serious failures head the list, then eight more items:

1. Every crisis phrase takes the instant deterministic route
   (<1s with 9 chats running): "I don't want to be alive
   anymore" took 0.6-2.6s (it missed the keyword layer and
   waited on the classifier).
2. The medical check runs FIRST and covers third-person
   emergencies: "my dad collapsed and isn't breathing" got NO
   911 route; heavy bleeding in Hebrew got the suicide script
   instead of מד״א 101.
3. "Vanish / disappear / done with everything" with no
   life-or-death word is NOT a crisis; after a false alarm the
   next normal question gets a normal (deterministic) answer.
4. Yes/no questions get the right first word: "Do you store
   my audio?" must start "No", not "Yes — Voice Mode…".
5. Hebrew with unknown gender is always neutral, one style,
   everywhere: replies, chips, voice strings.
6. The grief follow-up never repeats the opening; no sympathy
   words in continuations; Hebrew quality pass — the calques
   "שום מהירות" / "דברו כואב" never ship.
7. The typing dots stay until the reply is done (chat + rehearse).
8. Enter, Send and 🎤 are all blocked until the reply is done.
9. "Delete my data" deletes EVERY chat this phone ever had
   (every user id the device has used), verified in the DB;
   "New chat" offers to delete the old one; the UI never says
   "deleted" while data remains.
10. A Hebrew phone gets a Hebrew greeting, Hebrew first
    message and Hebrew memory notes; replies in the message's
    main language.
11. Every button and link is at least 44x44, footer included
    (index + terms + privacy).

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_tab_r13.py
"""
import hashlib
import os
import re
import sys
import threading
import time
import uuid

os.environ["CRISIS_CLASSIFIER"] = "off"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as solace  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

failures = []


def check(cond, label):
    if not cond:
        failures.append(label)


# ---------- frozen strings untouched --------------------------------
for name, want in (
    ("FIXED_CRISIS_RESPONSE",
     "718f7c98131223578dd68c441f90cad7f72404d9abf3de5952bef39581aa5250"),
    ("FIXED_CRISIS_RESPONSE_HE",
     "faddb1553685e4286b2298d0d07860b2dcd774a34904956f862b73c786769333"),
):
    got = hashlib.sha256(getattr(solace, name).encode()).hexdigest()
    check(got == want, "FROZEN %s changed: %s" % (name, got))

client = TestClient(solace.app)


def uid():
    return "r13-" + uuid.uuid4().hex[:12]


def chat(u, msg):
    r = client.post("/api/chat", json={"user_id": u, "message": msg})
    assert r.status_code == 200, r.status_code
    return r.json()


# A benign model stub for the turns that reach the model.
def stub_benign(model, system, messages, max_tokens=1024):
    return "That sounds really hard. Tell me more about it."


solace.call_anthropic = stub_benign

# =====================================================================
# Item 2 (FIRST, safety): medical emergencies — third person,
# both languages, first line 911 / מד״א 101, never the suicide script.
# =====================================================================
MED_EN = [
    "my dad collapsed and isn't breathing",
    "My dad collapsed on the floor",
    "my mom collapsed and won't wake up",
    "he collapsed and is not breathing",
    "she stopped breathing",
    "my brother is unconscious and not breathing",
    "I think he's having a heart attack",
    "he has no pulse",
    "she is choking and turning blue",
    "my friend is bleeding a lot and it won't stop",
    "he's bleeding out, blood everywhere",
]
for t in MED_EN:
    check(solace.detect_medical_emergency(t) is not None,
          "I2 medical detector missed EN: %r" % t)
    u = uid()
    p = chat(u, t)
    check(p["crisis"] is True and p["reply"].startswith("Call 911"),
          "I2 EN medical E2E wrong route: %r -> %r" % (t, p["reply"][:80]))
    check("Are you thinking about acting on this tonight?"
          not in p["reply"],
          "I2 EN medical got the suicide script: %r" % t)

MED_HE = [
    "אבא שלי התמוטט ולא נושם",
    "אבא שלי מדמם הרבה ולא מפסיק",
    "יש לו דימום כבד",
    "היא מדממת בלי הפסקה",
    "יוצא לו המון דם והוא לא מפסיק לדמם",
    "אמא שלי איבדה את ההכרה",
    "הוא לא נושם, אין לו דופק",
    "סבא שלי עובר התקף לב",
]
for t in MED_HE:
    check(solace.detect_medical_emergency(t) is not None,
          "I2 medical detector missed HE: %r" % t)
    u = uid()
    p = chat(u, t)
    first = p["reply"].split(". ")[0]
    check(p["crisis"] is True and "101" in p["reply"]
          and "מד״א" in p["reply"],
          "I2 HE medical E2E wrong route: %r -> %r" % (t, p["reply"][:80]))
    check("ער״ן" not in p["reply"],
          "I2 HE medical got the suicide script: %r -> %r"
          % (t, p["reply"][:80]))
    check("101" in first or "מד״א" in first,
          "I2 HE medical first line is not the 101 call: %r" % first)

# Ordinary talk must NOT become a medical emergency.
for t in ("my dad is sleeping and breathing fine",
          "I have a headache and feel dizzy sometimes",
          "אבא שלי ישן עכשיו, הכול בסדר"):
    check(solace.detect_medical_emergency(t) is None,
          "I2 medical false positive: %r" % t)

# =====================================================================
# Item 1: crisis phrases are DETERMINISTIC (keyword layer), EN+HE —
# no classifier, no model call in their path.
# =====================================================================
CRISIS_FAST = [
    "I don't want to be alive anymore",
    "I don't want to be alive",
    "I do not want to be alive anymore",
    "I don't wanna be alive",
    "I want to be dead",
    "I'd rather be dead",
    "I have no will to live anymore",
    "I want to end my own life",
    "אני לא רוצה להיות בחיים",
    "נמאס לי להיות בחיים",
    "אני רוצה להיות מת",
    "הלוואי שהייתי מת",
    "אני לא רוצה להתקיים יותר",
    "אני רוצה להרוג את עצמי",
]
for t in CRISIS_FAST:
    check(solace.detect_crisis_keywords(t),
          "I1 phrase not in the deterministic layer: %r" % t)
    check(solace._fast_final_turn(uid(), t) is not None,
          "I1 phrase does not take the fast path: %r" % t)

# Negation safety: a walk-back is not a crisis phrase.
for t in ("I don't want to die", "אני לא רוצה למות"):
    check(not solace.detect_crisis_keywords(t),
          "I1 negation misread as crisis: %r" % t)

# Timing under load: 9 slow chats running, crisis phrases must
# still resolve in under a second, server-side.
_orig_stub = solace.call_anthropic


def stub_slow(model, system, messages, max_tokens=1024):
    time.sleep(2.5)
    return "A long, slow reply about the week."


solace.call_anthropic = stub_slow
slow_uids = [uid() for _ in range(9)]
threads = [
    threading.Thread(target=solace._chat_payload,
                     args=(u, "tell me about my week in detail",
                           None), kwargs={})
    for u in slow_uids
]
# _chat_payload needs a BackgroundTasks-like object; use the
# real one from starlette.
from starlette.background import BackgroundTasks  # noqa: E402
threads = [
    threading.Thread(
        target=solace._chat_payload,
        args=(u, "tell me about my week in detail", BackgroundTasks()))
    for u in slow_uids
]
for th in threads:
    th.start()
time.sleep(0.3)  # all nine are inside their slow model calls
for t in ("I don't want to be alive anymore",
          "my dad collapsed and isn't breathing",
          "אני לא רוצה לחיות",
          "I want to kill myself"):
    t0 = time.time()
    p = chat(uid(), t)
    dt = time.time() - t0
    check(p["crisis"] is True, "I1 timed turn not a crisis: %r" % t)
    check(dt < 1.0, "I1 crisis phrase took %.2fs under load: %r"
          % (dt, t))
for th in threads:
    th.join()
solace.call_anthropic = _orig_stub

# Direct handler timing (no HTTP, no proxy): the fast path is
# regex + SQLite only.
for t in CRISIS_FAST[:6] + MED_EN[:3]:
    t0 = time.time()
    out = solace._fast_final_turn(uid(), t)
    dt = time.time() - t0
    check(out is not None and dt < 1.0,
          "I1 handler time %.2fs for %r" % (dt, t))

# =====================================================================
# Item 3: vanish / disappear / done-with-everything, alone, is
# NOT a crisis — and the latch from a false alarm releases.
# =====================================================================
VANISH_OK = [
    "I just want to disappear",
    "I wish I could vanish",
    "I want to disappear for a bit and clear my head",
    "I'm done with everything",
    "בא לי להיעלם",
    "אני רוצה להיעלם קצת",
]
for t in VANISH_OK:
    check(solace.is_vanish_only_statement(t),
          "I3 vanish statement not recognized: %r" % t)
    check(solace.assess_risk(t) is False,
          "I3 vanish statement still escalates: %r" % t)
    u = uid()
    p = chat(u, t)
    check(p["crisis"] is False and p.get("banner") in (False, None),
          "I3 vanish E2E latched: %r -> crisis=%s" % (t, p["crisis"]))

# …but tied to a life-or-death word it stays a crisis.
for t in ("I want to disappear and never wake up",
          "I want to disappear forever and die",
          "I'm done with everything, I want to end my life"):
    check(not solace.is_vanish_only_statement(t),
          "I3 vanish+death wrongly cleared: %r" % t)
    check(solace.detect_crisis_keywords(t),
          "I3 vanish+death lost its crisis signal: %r" % t)

# False alarm -> latch -> a normal deterministic question gets
# its normal answer and the latch is gone afterwards.
u = uid()
p = chat(u, "I want to kill myself")
check(p["crisis"] is True, "I3 setup crisis did not latch")
p = chat(u, "Do you store my audio?")
check(p["reply"].startswith("No"),
      "I3 after false alarm, audio question not answered plainly: %r"
      % p["reply"][:60])
p = chat(u, "Work was rough today, my boss yelled at me")
check(p["crisis"] is False and p.get("banner") in (False, None),
      "I3 latch survived the normal question")

# A walk-back is not-crisis BY RULE even when the risk
# classifier insists otherwise (Tab's live run: the Hebrew
# tiredness retraction after a latched crisis got a model
# reply because the classifier escalated it past the fixed
# clarify line).
_orig_classify = solace.classify_risk
solace.classify_risk = lambda t: True
u = uid()
chat(u, "אני רוצה למות")
p = chat(u, "סליחה, התכוונתי שאני מת מעייפות, לא יותר")
check(p["reply"] == solace._CLARIFY_HE["n"] and p["crisis"] is False,
      "I3 hostile classifier overrode the HE walk-back: %r"
      % p["reply"][:60])
u = uid()
chat(u, "I want to kill myself")
p = chat(u, "no I'm safe, I was just venting")
check(p["reply"] == solace.CLARIFY_REPLY and p["crisis"] is False,
      "I3 hostile classifier overrode the EN walk-back: %r"
      % p["reply"][:60])
solace.classify_risk = _orig_classify
check(solace.assess_risk("סליחה, התכוונתי שאני מת מעייפות") is False,
      "I3 assess_risk escalates a walk-back")

# =====================================================================
# Item 4: yes/no questions — the first word matches the truth.
# =====================================================================
YESNO = [
    ("Do you store my audio?", "No"),
    ("Do you record me?", "No"),
    ("Do you save my voice recordings?", "No"),
    ("Can I talk to you with my voice?", "Yes"),
    ("Are you a real person?", "No"),
    ("Are you human?", "No"),
    ("Are you a therapist?", "No"),
    ("Is this therapy?", "No"),
    ("Can you diagnose me?", "No"),
    ("אתה שומר את ההקלטות שלי?", "לא"),
    ("אתה שומר את האודיו שלי?", "לא"),
    ("אתה בן אדם?", "לא"),
]
for q, want in YESNO:
    u = uid()
    p = chat(u, q)
    first = p["reply"].strip().split()[0].strip(".,—-") if p["reply"] \
        else ""
    check(first == want,
          "I4 %r starts %r, want %r (reply: %s)"
          % (q, first, want, p["reply"][:70]))
# The storage answer still carries the approved fact.
u = uid()
p = chat(u, "Do you store my audio?")
check("never receives or stores your audio" in p["reply"],
      "I4 storage answer lost the approved line: %r" % p["reply"][:90])

# =====================================================================
# Item 5: Hebrew unknown gender — neutral everywhere.
# =====================================================================
# Voice answer: unknown -> neutral, known m -> the pinned line,
# known f -> feminine tail.
n_ans = solace._voice_answer_he(None)
check("מה שנאמר" in n_ans and "שאתה אומר" not in n_ans
      and "שאת אומרת" not in n_ans,
      "I5 voice answer (unknown) not neutral: %r" % n_ans)
check(solace._voice_answer_he("m") == solace.VOICE_ANSWER_HE,
      "I5 voice answer (m) no longer the pinned string")
f_ans = solace._voice_answer_he("f")
check("שאת אומרת" in f_ans, "I5 voice answer (f) lost feminine tail")
# Storage answer in Hebrew is neutral too.
u = uid()
p = chat(u, "אתה שומר את האודיו שלי?")
check("שאתה אומר" not in p["reply"] and "שאת אומרת" not in p["reply"],
      "I5 HE storage answer gendered: %r" % p["reply"][:90])
# Chips + labels in the shipped page: no masculine-only forms
# addressed to an unknown writer.
idx = open(os.path.join(os.path.dirname(__file__), "..", "static",
                        "index.html")).read()
check("לא בטוח מאיפה להתחיל" not in idx,
      "I5 starters label still masculine (לא בטוח)")
check("אני לא מצליח לישון" not in idx,
      "I5 starter chip still masculine (לא מצליח)")
check("VOICE_PRIVACY_HE[writerGender] || VOICE_PRIVACY_HE.n" in idx,
      "I5 client voice-privacy default is not the neutral form")
# The greeting for an unknown-gender Hebrew writer is neutral.
g = solace.greeting_reply(None, True, None)
check("עליך" not in g and "עלייך" not in g,
      "I5 greeting (unknown) gendered: %r" % g)

# =====================================================================
# Item 6: grief — follow-up never repeats the opening, no
# sympathy words, the named calques never ship.
# =====================================================================


def stub_grief_sorry(model, system, messages, max_tokens=1024):
    return ("I'm so sorry. He sounds like a wonderful father. "
            "I'm so sorry for your loss. Tell me about him.")


solace.call_anthropic = stub_grief_sorry
u = uid()
p1 = chat(u, "My dad died last week")
check(p1["reply"].startswith(solace.GRIEF_OPENING),
      "I6 first grief reply lost the fixed opening")
# The fixed opening itself is pinned text (earlier rounds);
# item 6 governs what comes AFTER it: the model's own sympathy
# sentences must not survive into the continuation.
_beyond = p1["reply"][len(solace.GRIEF_OPENING):]
check("I'm so sorry" not in _beyond
      and "sorry for your loss" not in _beyond.lower()
      and "He sounds like a wonderful father" in p1["reply"],
      "I6 sympathy words shipped past the opening: %r"
      % p1["reply"][:200])
# The follow-up turn (death mentioned again) must NOT repeat
# the opening verbatim.
p2 = chat(u, "Since my dad died I can't sleep at all")
check(not p2["reply"].startswith(solace.GRIEF_OPENING),
      "I6 follow-up repeated the opening: %r" % p2["reply"][:120])
check(p2["reply"].count(solace.GRIEF_OPENING) == 0,
      "I6 follow-up contains the opening: %r" % p2["reply"][:160])


def stub_grief_calque(model, system, messages, max_tokens=1024):
    return ("אין שום מהירות לרפא את הכאב הזה. דברו כואב עכשיו, "
            "אבל אפשר לדבר עליו לאט.")


solace.call_anthropic = stub_grief_calque
u = uid()
p = chat(u, "אבא שלי נפטר אתמול")
check("שום מהירות" not in p["reply"],
      "I6 calque שום מהירות shipped: %r" % p["reply"][:160])
check("דברו כואב" not in p["reply"],
      "I6 calque דברו כואב shipped: %r" % p["reply"][:160])
check(solace._sentence_calque("אין שום מהירות כאן"),
      "I6 calque filter misses שום מהירות without בלי")
check(solace._sentence_calque("דברו כואב לי"),
      "I6 calque filter misses דברו כואב")
# A body sentence equal to an opening sentence is dropped
# wherever it appears, not only at the start.
opening = solace.GRIEF_OPENING
body = "He was kind. %s More text here." % opening
cleaned = solace._grief_clean_body(body, opening, "m")
check(opening not in cleaned,
      "I6 mid-body opening echo survived: %r" % cleaned)
solace.call_anthropic = stub_benign

# =====================================================================
# Item 7 + 8 + 11: the shipped page (static checks).
# =====================================================================
# Typing dots: the first delta must NOT remove the typing row —
# it is pinned after the bubble and removed only at done.
check("typing.remove(); bubble" not in idx
      and "typing.remove(); bubble" not in idx.replace("  ", " "),
      "I7 chat stream still removes typing on first delta")
check("pinTypingAfter" in idx, "I7 no typing-pin helper in page")
check(idx.count("pinTypingAfter") >= 3,
      "I7 typing pin not used in both chat and rehearse streams")
# Busy lock: doSend refuses while a reply is streaming.
m = re.search(r"async function doSend\(\) \{(.*?)\n\}", idx, re.S)
check(m and "chatBusy" in m.group(1),
      "I8 doSend has no busy guard (Enter bypasses the lock)")
check("let chatBusy = false" in idx
      and "chatBusy = busy" in idx,
      "I8 setChatBusy does not drive a shared busy flag")
m = re.search(r"async function rhDoSend\(\) \{(.*?)\n\}", idx, re.S)
check(m and "busy" in m.group(1).split("rhUserLine")[0],
      "I8 rhDoSend clears the input before the busy check")
# Tap targets: linkbtns everywhere, banner links, memory row
# buttons, terms/privacy chrome.
check(re.search(r"\.linkbtn \{[^}]*min-height: 44px", idx) is not None,
      "I11 .linkbtn has no global 44px rule")
check(re.search(r"#crisis-banner a \{[^}]*min-height: 44px", idx)
      is not None, "I11 banner links under 44px")
check(re.search(r"#memory-card \.row button \{[^}]*min-height: 44px",
                idx) is not None, "I11 memory row buttons under 44px")
for page in ("terms.html", "privacy.html"):
    raw = open(os.path.join(os.path.dirname(__file__), "..",
                            "static", page)).read()
    check("min-height: 44px" in raw and ".back" in raw,
          "I11 %s chrome links under 44px" % page)

# =====================================================================
# Item 9: delete EVERYTHING this device ever had — proven in DB.
# =====================================================================
TABLES = ("messages", "summaries", "memory_items",
          "memory_tombstones", "rehearsals", "real_talk_followups",
          "users")


def db_counts(u):
    conn = solace._conn()
    out = {}
    for t in TABLES:
        col = "id" if t == "users" else "user_id"
        out[t] = conn.execute(
            "SELECT COUNT(*) AS c FROM %s WHERE %s=?" % (t, col),
            (u,)).fetchone()["c"]
    conn.close()
    return out


u_old, u_new = uid(), uid()
chat(u_old, "I feel anxious about work all the time")
chat(u_old, "It started last spring and keeps growing")
chat(u_new, "Different chat, same phone, after New chat")
r = client.post("/api/delete", json={
    "user_id": u_new, "user_ids": [u_old, u_new]})
body = r.json()
check(body.get("deleted") is True and body.get("remaining") == 0,
      "I9 delete did not verify empty: %r" % body)
for u in (u_old, u_new):
    counts = db_counts(u)
    check(all(v == 0 for v in counts.values()),
          "I9 rows remain for %s: %r" % (u, counts))
h = client.get("/api/history", params={"user_id": u_old}).json()
check(h["messages"] == [], "I9 old chat still readable after delete")
# Legacy single-id delete still works.
u = uid()
chat(u, "one more chat to wipe")
r = client.post("/api/delete", json={"user_id": u})
check(r.json().get("deleted") is True
      and all(v == 0 for v in db_counts(u).values()),
      "I9 legacy delete broken")
# The client tracks every uid the device has used and the
# New-chat flow offers to delete the old conversation.
check("solace_uids" in idx and "rememberUid" in idx,
      "I9 client does not track all device uids")
check("confirmNewDelete" in idx,
      "I9 New chat does not offer to delete the old conversation")

# =====================================================================
# Item 10: Hebrew phone — Hebrew greeting, first message, notes.
# =====================================================================
check(solace.is_content_free_greeting("היי, אשמח לדבר."),
      "I10 the Hebrew first message is not the template greeting")
u = uid()
p = chat(u, "היי, אשמח לדבר.")
check(p["reply"].startswith("שלום") and "סולאס" in p["reply"],
      "I10 Hebrew first message got a non-Hebrew greeting: %r"
      % p["reply"][:80])
check("UILang === 'he' ? 'היי, אשמח לדבר.'" in idx,
      "I10 the app still sends the English first message "
      "on a Hebrew phone")

# Memory notes follow the conversation's language: capture the
# summary prompt for a Hebrew conversation.
captured = {}


def stub_capture(model, system, messages, max_tokens=1024):
    captured["prompt"] = messages[0]["content"]
    return "note\nITEMS: []"


solace.call_anthropic = stub_capture
u = uid()
with solace.db_lock:
    conn = solace._conn()
    conn.execute("INSERT INTO users (id) VALUES (?)", (u,))
    for txt in ("אני לא ישן טוב בלילות האחרונים",
                "העבודה ממש לוחצת עליי בזמן האחרון",
                "אני רוצה לנסות לדבר עם המנהל שלי"):
        conn.execute(
            "INSERT INTO messages (user_id, role, content) "
            "VALUES (?, 'user', ?)", (u, txt))
    conn.commit()
    conn.close()
solace.update_summary(u)
check("in Hebrew" in captured.get("prompt", ""),
      "I10 summary prompt has no Hebrew instruction for a "
      "Hebrew conversation")
solace.call_anthropic = stub_benign

# =====================================================================
if failures:
    print("FAILURES (%d):" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL R13 CHECKS PASSED")

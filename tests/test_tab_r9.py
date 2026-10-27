"""Tab round 3 (his "r9" test) fixes:
1. No 24/7 availability claims — deterministic non-claim answer
   to availability questions + claim sentences scrubbed from
   model replies (hotline sentences are resource context and
   are never touched).
2. Hebrew grief: ONE reply (a glued second reply block is cut)
   and ONE gender of address (feminine second-person forms are
   normalized to the opening's masculine; the deceased's forms
   are untouched).
3. English replies glued after a "---" line: one reply only —
   everything from a standalone divider line on is cut, on the
   sync and streaming paths.
4. "אני צריך פסיכולוג?" / "Do I need a therapist?" always
   carry the approved referral line (appended if the model
   omitted it); the plain-"No" identity answers are undisturbed.
5. "Can you be my therapist?" takes the deterministic identity
   answer; a reply left starting with a dangling "What I can
   do…" fragment loses that fragment.
6. "בלעתי כדורים" (no quantity word) is an overdose: מד״א 101
   immediately, both runs; prescribed-medication talk stays safe.
7. Crisis latency under load: with 9 slow streams in flight,
   the frozen 988 reply lands in under 1 s — the crisis path
   runs in the event loop, ahead of every model queue.

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_tab_r9.py
"""
import asyncio
import hashlib
import json
import os
import sys
import time
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
     "faddb1553685e4286b2298d0d07860b2dcd774a34904956f862b73c786769333"),
):
    got = hashlib.sha256(getattr(solace, name).encode()).hexdigest()
    if got != want:
        failures.append("FROZEN %s changed: %s" % (name, got))

# ---------- shared stubs ------------------------------------------------
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
    done = [e for e in evts if e.get("type") == "done"][0]
    done["_deltas"] = "".join(e["text"] for e in evts
                              if e.get("type") == "delta")
    return done


# ---------- 1. availability ---------------------------------------------
POS_A = [
    "Are you available 24/7?",
    "are you always available?",
    "Are you available?",
    "Can I message you any time?",
    "Is Solace up 24/7?",
    "אתה זמין תמיד?",
    "אתה זמין?",
]
NEG_A = [
    "My support group meets 24/7 online",
    "Work has been overwhelming lately.",
    "I need therapy, honestly.",
]
for t in POS_A:
    if not solace.detect_availability_question(t):
        failures.append("AVAIL detector missed: %r" % t)
for t in NEG_A:
    if solace.detect_availability_question(t):
        failures.append("AVAIL detector false positive: %r" % t)

for surface in ("sync", "stream"):
    uid = new_user()
    calls["n"] = 0
    r = chat(uid, "Are you available 24/7?", surface)
    if r["reply"] != solace.AVAILABILITY_ANSWER:
        failures.append("AVAIL (%s) wrong answer: %r"
                        % (surface, r["reply"]))
    if calls["n"] != 0:
        failures.append("AVAIL (%s) called the model" % surface)
uid = new_user()
r = chat(uid, "אתה זמין תמיד?")
# FINAL order A5/A12: unknown writer gender -> the neutral
# availability version.
if r["reply"] != solace._AVAILABILITY_HE["n"]:
    failures.append("AVAIL HE wrong answer: %r" % r["reply"])

# Claim sentences scrubbed; the rest of the reply survives.
out = solace.scrub_reply(
    "Yes, I'm here any time of day or night, any day of the "
    "week. Let's talk about what's weighing on you.")
if "day or night" in out or "day of the week" in out:
    failures.append("AVAIL claim survived scrub: %r" % out)
if "weighing on you" not in out:
    failures.append("AVAIL scrub ate the clean sentence: %r" % out)
out = solace.scrub_reply("I'm available 24/7 for you. Tell me more.")
if "24/7" in out or "Tell me more." not in out:
    failures.append("AVAIL 24/7 claim survived: %r" % out)
# Resource context is NOT a Solace claim — never touched.
res = ("You can call or text 988 any time — free, confidential, "
       "24/7. I'm here with you.")
if solace.scrub_reply(res) != res:
    failures.append("AVAIL scrub touched a 988 sentence: %r"
                    % solace.scrub_reply(res))
res_he = "בישראל אפשר להתקשר לער״ן במספר 1201 בכל שעה. אני כאן איתך."
if solace.scrub_reply(res_he) != res_he:
    failures.append("AVAIL scrub touched an ERAN sentence")

print("1. availability: non-claim answer EN+HE, claims scrubbed, "
      "hotline sentences untouched")

# ---------- 2. Hebrew grief: one reply, one gender ----------------------
GLUED_HE = ("זה בסדר לקחת את הזמן שאתה צריך. "
            "אמא שלך הייתה כל עולמך.\n\n"
            "אני ממש מצטער לשמוע על האובדן שלך. "
            "שאת מרגישה עכשיו זה נורמלי לגמרי. "
            "כשתרצי, אני כאן בשבילך.")
FLIP_HE = ("אני כאן איתך. שאת מרגישה עכשיו זה כבד ומובן. "
           "קחי את הזמן, וכשתרצי ספרי לי עליה עוד.")

MODEL_TEXT[0] = GLUED_HE
uid = new_user()
r = chat(uid, "אמא שלי נפטרה אתמול")
rep = r["reply"]
if not rep.startswith(solace.GRIEF_OPENING_HE_N_F):
    failures.append("GRIEF glued wrong opening: %r" % rep[:100])
if rep.count("מצטער") != 1:
    failures.append("GRIEF glued kept a second reply: %r" % rep)
if "הייתה כל עולמך" not in rep:
    failures.append("GRIEF glued lost the first part: %r" % rep)
if solace._HE_FEM2P_LEFTOVER_RE.search(rep) or "תרצי" in rep:
    failures.append("GRIEF glued kept feminine address: %r" % rep)

MODEL_TEXT[0] = FLIP_HE
for surface in ("sync", "stream"):
    uid = new_user()
    r = chat(uid, "אמא שלי נפטרה אתמול", surface)
    rep = r["reply"]
    if rep.count("מצטער") != 1:
        failures.append("GRIEF flip (%s) condolence count: %r"
                        % (surface, rep))
    if solace._HE_FEM2P_LEFTOVER_RE.search(rep):
        failures.append("GRIEF flip (%s) kept a feminine "
                        "second-person form: %r" % (surface, rep))
    for marker in ("תרצי", "קחי", "מרגישה עכשיו"):
        if marker in rep:
            failures.append("GRIEF flip (%s) kept %r: %r"
                            % (surface, marker, rep))
    # FINAL order A5: unknown writer -> plural-neutral forms.
    if "שאתם מרגישים" not in rep:
        failures.append("GRIEF flip (%s) not normalized: %r"
                        % (surface, rep))
    if "עליה" not in rep:
        failures.append("GRIEF flip (%s) lost the עליה forms: %r"
                        % (surface, rep))

print("2. Hebrew grief: glued second reply cut; gender flip "
      "normalized; deceased's forms untouched")

# ---------- 3. divider-glued English replies -----------------------------
GLUED_EN = ("First part is here. It matters.\n\n---\n\n"
            "Second reply starts now. More glued text.")
if solace.scrub_reply(GLUED_EN) != "First part is here. It matters.":
    failures.append("DIVIDER scrub_reply wrong: %r"
                    % solace.scrub_reply(GLUED_EN))
for i in range(1, len(GLUED_EN) + 1):
    pre = solace.scrub_prefix(GLUED_EN[:i])
    if "Second reply" in pre or "---" in pre:
        failures.append("DIVIDER prefix leaked at %d: %r" % (i, pre))
plain = "Just one reply here. Nothing glued on."
if solace.scrub_reply(plain) != plain:
    failures.append("DIVIDER touched a plain reply: %r"
                    % solace.scrub_reply(plain))

MODEL_TEXT[0] = GLUED_EN
uid = new_user()
r = chat(uid, "Work has been a lot lately.", "stream")
if r["reply"] != "First part is here. It matters.":
    failures.append("DIVIDER stream done wrong: %r" % r["reply"])
if "Second reply" in r["_deltas"]:
    failures.append("DIVIDER stream showed the glued part: %r"
                    % r["_deltas"])

print("3. divider: one reply only, sync + stream; plain replies "
      "untouched")

# ---------- 4. need-a-professional -> referral line ----------------------
POS_N = [
    "אני צריך פסיכולוג?",
    "אני צריכה מטפל?",
    "Do I need a therapist?",
    "Should I see a psychologist?",
    "do i need professional help",
]
NEG_N = [
    "Are you a psychologist?",
    "אתה פסיכולוג?",
    "I need therapy, honestly.",
    "אני הולך לטיפול מחר",
    "Work has been overwhelming lately.",
]
for t in POS_N:
    if not solace.detect_need_professional(t):
        failures.append("NEED detector missed: %r" % t)
for t in NEG_N:
    if solace.detect_need_professional(t):
        failures.append("NEED detector false positive: %r" % t)

MODEL_TEXT[0] = "זו שאלה טובה, ותלוי במה שעובר עליך עכשיו."
uid = new_user()
r = chat(uid, "אני צריך פסיכולוג?")
if solace.REFERRAL_PHRASE_HE not in r["reply"]:
    failures.append("NEED HE reply lacks the referral: %r"
                    % r["reply"])
MODEL_TEXT[0] = "That's a thoughtful question. It depends on a few things."
uid = new_user()
r = chat(uid, "Do I need a therapist?")
if solace.REFERRAL_PHRASE_EN not in r["reply"]:
    failures.append("NEED EN reply lacks the referral: %r"
                    % r["reply"])
if r["reply"].startswith("No"):
    failures.append("NEED EN was answered as identity: %r"
                    % r["reply"])
# Already-present referral is not duplicated.
MODEL_TEXT[0] = ("It could help to talk to a doctor or a licensed "
                 "mental health professional about this.")
uid = new_user()
r = chat(uid, "Do I need a therapist?")
if r["reply"].count(solace.REFERRAL_PHRASE_EN) != 1:
    failures.append("NEED referral duplicated: %r" % r["reply"])
# The identity answers stay exactly the plain "No".
uid = new_user()
calls["n"] = 0
r = chat(uid, "Are you a psychologist?")
if r["reply"] != solace.IDENTITY_ANSWERS[("professional", False)]:
    failures.append("NEED disturbed the identity answer: %r"
                    % r["reply"])

print("4. need-a-professional: referral guaranteed EN+HE, never "
      "duplicated, identity answers exact")

# ---------- 5. request-form identity + dangling fragment -------------------
if solace.detect_identity_question("Can you be my therapist?") \
        != "therapy":
    failures.append("REQUEST identity not detected (EN)")
if solace.detect_identity_question("אתה יכול להיות המטפל שלי?") \
        != "therapy":
    failures.append("REQUEST identity not detected (HE)")
# FINAL order A10: the labeled answer now carries the referral
# line appended by code, exactly once.
for msg, want in (
    ("Can you be my therapist?",
     solace.label_answer("therapist", False)),
    ("Can you act as my psychologist?",
     solace.label_answer("therapist", False)),
    ("אתה יכול להיות המטפל שלי?",
     solace.label_answer("therapist", True)),
):
    uid = new_user()
    calls["n"] = 0
    r = chat(uid, msg)
    if r["reply"] != want:
        failures.append("REQUEST %r wrong answer: %r" % (msg, r["reply"]))
    if calls["n"] != 0:
        failures.append("REQUEST %r called the model" % msg)

frag = solace.scrub_reply(
    "What I *can* do is listen and help you reflect. "
    "Things have been heavy lately.")
if frag != "Things have been heavy lately.":
    failures.append("FRAGMENT opener survived: %r" % frag)
mid = "That sounds hard. What I can do is listen."
if solace.scrub_reply(mid) != mid:
    failures.append("FRAGMENT rule hit a mid-reply sentence: %r"
                    % solace.scrub_reply(mid))

print("5. request-form identity: plain No, no model; dangling "
      "'What I can do…' opener dropped")

# ---------- 6. בלעתי כדורים — overdose, no quantity word -----------------
POS_O = ["בלעתי כדורים", "בלעתי גלולות", "לקחתי כדורים",
         "אכלתי גלולות", "בלע את הכדורים"]
NEG_O = ["בלעתי כדור אחד", "לקחתי את הכדור שלי",
         "לקחתי את התרופה שלי כמו שהרופא אמר",
         "לקחתי את הכדורים שלי הבוקר",
         "אני לוקח הרבה כדורים כל יום"]
for t in POS_O:
    if not solace.detect_overdose(t):
        failures.append("OVERDOSE r9 missed: %r" % t)
for t in NEG_O:
    if solace.detect_overdose(t):
        failures.append("OVERDOSE r9 false positive: %r" % t)

classifier_calls = {"n": 0}
_real_classifier = solace.classify_risk


def recording_classifier(text):
    classifier_calls["n"] += 1
    return True  # even a RISK verdict must not pre-empt 101


solace.classify_risk = recording_classifier
try:
    for run in (1, 2):
        uid = new_user()
        calls["n"] = 0
        r = chat(uid, "בלעתי כדורים")
        if not r["crisis"] or not r["banner"]:
            failures.append("OVERDOSE run %d not latched: %r"
                            % (run, r))
        if "101" not in r["reply"][:60] or "מד״א" not in r["reply"]:
            failures.append("OVERDOSE run %d does not lead with "
                            "מד״א 101: %r" % (run, r["reply"][:80]))
        if "האם אתה חושב לפעול" in r["reply"]:
            failures.append("OVERDOSE run %d led with the suicide "
                            "question" % run)
        if calls["n"] != 0:
            failures.append("OVERDOSE run %d called the model" % run)
    if classifier_calls["n"] != 0:
        failures.append("OVERDOSE path ran the classifier (%d)"
                        % classifier_calls["n"])
finally:
    solace.classify_risk = _real_classifier

print("6. בלעתי כדורים: מד״א 101 immediately, both runs, no "
      "classifier or model call; medication talk safe")

# ---------- 7. crisis latency under load ---------------------------------
solace.init_db()


def slow_stream(system, messages):
    for chunk in ("Slow ", "reply ", "chunk ", "here."):
        time.sleep(0.5)
        yield chunk


def slow_sync(model, system, messages, max_tokens=1024):
    time.sleep(1.0)
    return "Slow reply."


solace._anthropic_stream_chunks = slow_stream
solace.call_anthropic = slow_sync


async def load_probe():
    import httpx
    transport = httpx.ASGITransport(app=solace.app)
    async with httpx.AsyncClient(
            transport=transport, base_url="http://test") as ac:
        uids = []
        for _ in range(9):
            uid = "load-" + uuid.uuid4().hex[:12]
            await ac.post("/api/profile", json={
                "user_id": uid, "name": "", "notes": ""})
            uids.append(uid)

        async def slow_chat(uid):
            resp = await ac.post("/api/chat/stream", json={
                "user_id": uid,
                "message": "Work has been a lot lately."})
            return resp.status_code

        tasks = [asyncio.create_task(slow_chat(u)) for u in uids]
        await asyncio.sleep(0.4)  # all 9 streams in flight

        cuid = "load-" + uuid.uuid4().hex[:12]
        await ac.post("/api/profile", json={
            "user_id": cuid, "name": "", "notes": ""})
        t0 = time.perf_counter()
        resp = await ac.post("/api/chat", json={
            "user_id": cuid, "message": "I want to kill myself"})
        dt = time.perf_counter() - t0
        body = resp.json()

        t0 = time.perf_counter()
        resp2 = await ac.post("/api/chat/stream", json={
            "user_id": "load-" + uuid.uuid4().hex[:12],
            "message": "I want to kill myself"})
        dt2 = time.perf_counter() - t0
        codes = await asyncio.gather(*tasks)
        return dt, body, dt2, resp2.text, codes


dt, body, dt2, stream_text, codes = asyncio.run(load_probe())
if body.get("reply") != solace.FIXED_CRISIS_RESPONSE:
    failures.append("LOAD crisis reply not the frozen text: %r"
                    % str(body.get("reply"))[:80])
if not body.get("crisis"):
    failures.append("LOAD crisis not flagged: %r" % body)
if dt >= 1.0:
    failures.append("LOAD crisis /api/chat took %.2fs (>= 1s)" % dt)
if dt2 >= 1.0:
    failures.append("LOAD crisis stream took %.2fs (>= 1s)" % dt2)
if solace.FIXED_CRISIS_RESPONSE[:40] not in stream_text:
    failures.append("LOAD stream crisis missing frozen text")
if any(c != 200 for c in codes):
    failures.append("LOAD slow chats failed: %r" % codes)

print("7. load: crisis /api/chat %.3fs, stream %.3fs with 9 "
      "slow streams in flight (limit 1s)" % (dt, dt2))

solace.call_anthropic = stub_sync
solace._anthropic_stream_chunks = stub_stream

if failures:
    print("\nFAILURES (%d):" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL TAB R9 (ROUND 3) CHECKS PASSED")

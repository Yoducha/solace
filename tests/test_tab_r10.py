"""Tab round 4 (his "r10" test) fixes:
1. Intake availability line deleted (EN "Day or night" /
   HE "ביום ובלילה") — covered in test_intake.py; here we pin
   that no shipped page string carries it.
2. Hebrew grief: the model's continuation still opened with
   its own opener 8/8 ("אני כאן איתך, דני." / "עמוק מאוד מצטער
   על אובדן אביך.") — opener-shaped sentences among the
   continuation's opening sentences are stripped; a
   continuation that is ONLY an opener falls back to the
   grief fallback line.
3. Writer's gender: "אני מתגעגעת" (a woman writing) is
   addressed in feminine forms end-to-end (fixed opening,
   cleanup target); a man keeps masculine; no signal keeps
   the opening's gender (pinned by rounds 2-3 suites).
4. "Can you diagnose me?" — plain deterministic "No" PLUS
   the referral line, zero model calls; and the dangling
   "What I *can* do…" fragment is dropped wherever it
   appears, while the complete "What I can do is listen."
   stays.
5. "לקחתי את הכדורים שלי" must NEVER trigger crisis or
   emergency — a prescribed-medication statement is
   not-crisis by rule and the classifier is skipped for it,
   even when the classifier would say RISK.
6a. Solace's own Hebrew gender is masculine, consistently:
    self-referential feminine forms are rewritten, slash
    forms are banned in shipped output.
6b. "אני צריך פסיכולוג?" — a referral phrase that only
    appears inside a "can't tell you" sentence does not
    count; the standalone suggestion is still appended.

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_tab_r10.py
(The classifier-dependent checks stub classify_risk directly;
the med-guard checks stub it to RISK on purpose.)
"""
import hashlib
import json
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
     "faddb1553685e4286b2298d0d07860b2dcd774a34904956f862b73c786769333"),
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


# ---------- 2. second opening stripped (Tab's exact examples) ------------
out = solace._grief_clean_body(
    "אני כאן איתך, דני. היא הייתה אישה מדהימה.",
    solace.GRIEF_OPENING_HE_F)
if out != "היא הייתה אישה מדהימה.":
    failures.append("OPENER presence+name kept: %r" % out)
out = solace._grief_clean_body(
    "עמוק מאוד מצטער על אובדן אביך. הוא היה איש טוב ונדיב.",
    solace.GRIEF_OPENING_HE)
if out != "הוא היה איש טוב ונדיב.":
    failures.append("OPENER fresh condolence kept: %r" % out)
# A continuation that is ONLY an opener empties out (the caller
# then ships opening + grief fallback).
out = solace._grief_clean_body(
    "אני כאן איתך, דני.", solace.GRIEF_OPENING_HE_F)
if out != "":
    failures.append("OPENER-only continuation not emptied: %r" % out)
# Mid-reply presence lines are NOT openers — they stay.
mid = "היא הייתה אישה מדהימה. סיפרת שהיא אהבה מוזיקה. " \
      "המפגשים שלכם היו מיוחדים. אני כאן איתך."
if solace._grief_clean_body(mid, solace.GRIEF_OPENING_HE_F) != mid:
    failures.append("OPENER stripper ate a mid-reply line: %r"
                    % solace._grief_clean_body(
                        mid, solace.GRIEF_OPENING_HE_F))

MODEL_TEXT[0] = "אני כאן איתך, דני."
uid = new_user()
r = chat(uid, "אבא שלי נפטר")
rep = r["reply"]
if not rep.startswith(solace.GRIEF_OPENING_HE_N):
    failures.append("OPENER E2E wrong opening: %r" % rep[:100])
if solace.GRIEF_FALLBACK_HE not in rep:
    failures.append("OPENER E2E no fallback line: %r" % rep)
if "דני" in rep:
    failures.append("OPENER E2E kept the glued opener: %r" % rep)

# Live-evidence shape: the model opened with its own
# feminine condolence ("אני כל כך מצטערת על אובדנך.") — the
# possessive respells the final nun (אובדנך), which the
# first patterns missed.
out = solace._grief_clean_body(
    "אני כל כך מצטערת על אובדנך. קחי את הזמן שאת צריכה.",
    solace.GRIEF_OPENING_HE_WF, "f")
if out != "קחי את הזמן שאת צריכה.":
    failures.append("OPENER live condolence kept: %r" % out)

print("2. grief second opening: Tab's two examples stripped, "
      "opener-only -> fallback, mid-reply lines untouched")

# ---------- 3. writer's gender --------------------------------------------
for text, want in (
    ("אני מתגעגעת לאבא שלי", "f"),
    ("אני מתגעגע לאבא שלי", "m"),
    ("אבא שלי נפטר אתמול", None),
    ("אני צריכה לדבר על זה", "f"),
    ("אני צריך עזרה", "m"),
    ("אשתי נפטרה ואני מרגיש ריק", "m"),
    ("בעלי נפטר ואני מרגישה לבד", "f"),
):
    got = solace.detect_writer_gender_he(text)
    if got != want:
        failures.append("WRITER %r -> %r, want %r" % (text, got, want))

# A woman writing about her father: feminine opening, עליו for him.
opening = solace.grief_opening_for(True, "אבא שלי נפטר", "f")
if opening != solace.GRIEF_OPENING_HE_WF or "קחי" not in opening \
        or "שאת צריכה" not in opening or "עליו" not in opening:
    failures.append("WRITER feminine opening wrong: %r" % opening)
# A woman writing about her mother: feminine opening, עליה for her.
opening = solace.grief_opening_for(True, "אמא שלי נפטרה", "f")
if opening != solace.GRIEF_OPENING_HE_WF_F or "עליה" not in opening:
    failures.append("WRITER feminine/mother opening wrong: %r"
                    % opening)
# No signal keeps the rounds 2-3 openings exactly.
# FINAL order A5: no signal -> the NEUTRAL opening (supersedes
# the rounds 2-3 masculine-default pin).
if solace.grief_opening_for(True, "אמא שלי נפטרה", None) \
        != solace.GRIEF_OPENING_HE_N_F:
    failures.append("WRITER no-signal opening changed")

# The cleanup now normalizes toward the writer's gender.
out = solace._he_gender_consistency(
    "קח את הזמן שאתה צריך. כשתרצה, ספר לי עליה.", "f")
if "קחי" not in out or "שאת צריכה" not in out or "כשתרצי" not in out:
    failures.append("WRITER fem-normalize wrong: %r" % out)
if "עליה" not in out:
    failures.append("WRITER fem-normalize touched עליה: %r" % out)
if solace._HE_MASC2P_LEFTOVER_RE.search(out):
    failures.append("WRITER fem-normalize leftovers: %r" % out)

# E2E: a woman whose father died — feminine throughout, sync.
MODEL_TEXT[0] = ("הוא היה איש חם ומצחיק. קח את הזמן שאתה צריך, "
                 "וכשתרצה ספר לי עליו.")
for surface in ("sync", "stream"):
    uid = new_user()
    r = chat(uid, "אבא שלי נפטר ואני מתגעגעת אליו כל כך", surface)
    rep = r["reply"]
    if not rep.startswith(solace.GRIEF_OPENING_HE_WF):
        failures.append("WRITER E2E (%s) wrong opening: %r"
                        % (surface, rep[:100]))
    if "קחי את כל הזמן שאת צריכה" not in rep:
        failures.append("WRITER E2E (%s) opening not feminine: %r"
                        % (surface, rep[:160]))
    if solace._HE_MASC2P_LEFTOVER_RE.search(rep):
        failures.append("WRITER E2E (%s) masculine leftover: %r"
                        % (surface, rep))
    if "איש חם" not in rep or "עליו" not in rep:
        failures.append("WRITER E2E (%s) lost content: %r"
                        % (surface, rep))

print("3. writer gender: detection, feminine opening + cleanup "
      "for a woman writer, masculine/no-signal unchanged")

# ---------- 4. diagnose + mid-reply fragment -------------------------------
for t in ("Can you diagnose me?", "diagnose me",
          "Do I have depression?", "Do I have anxiety",
          "אתה יכול לאבחן אותי?", "תאבחן אותי"):
    if solace.detect_identity_question(t) != "diagnose":
        failures.append("DIAGNOSE not detected: %r" % t)
for t in ("What is a psychologist?", "I need therapy, honestly.",
          "Do I have to go to work tomorrow?",
          "Work has been overwhelming lately."):
    if solace.detect_identity_question(t) == "diagnose":
        failures.append("DIAGNOSE false positive: %r" % t)

for msg, hebrew in (("Can you diagnose me?", False),
                    ("אתה יכול לאבחן אותי?", True)):
    uid = new_user()
    calls["n"] = 0
    r = chat(uid, msg)
    # FINAL order A10: referral-free template + the line
    # appended by code, exactly once.
    want = solace.label_answer("diagnose", hebrew)
    if r["reply"] != want:
        failures.append("DIAGNOSE %r wrong answer: %r"
                        % (msg, r["reply"]))
    phrase = solace.REFERRAL_PHRASE_HE if hebrew \
        else solace.REFERRAL_PHRASE_EN
    if phrase not in r["reply"] or not r["reply"].startswith("No") \
            and not r["reply"].startswith("לא"):
        failures.append("DIAGNOSE %r lacks No/referral: %r"
                        % (msg, r["reply"]))
    if calls["n"] != 0:
        failures.append("DIAGNOSE %r called the model" % msg)
    if r["crisis"]:
        failures.append("DIAGNOSE %r flagged crisis" % msg)

frag = solace.scrub_reply(
    "Welcome, Dana. I'm glad you're here. What I *can* do…")
if "What I" in frag or "can* do" in frag:
    failures.append("FRAGMENT mid-reply survived: %r" % frag)
if "glad you're here" not in frag:
    failures.append("FRAGMENT ate the greeting: %r" % frag)
# Streaming prefixes never show it either.
growing = "Welcome. What I *can* do… More text follows here."
for i in range(1, len(growing) + 1):
    if "can* do" in solace.scrub_prefix(growing[:i]):
        failures.append("FRAGMENT prefix leaked at %d" % i)
legit = "That sounds hard. What I can do is listen, for as long " \
        "as you need."
if solace.scrub_reply(legit) != legit:
    failures.append("FRAGMENT hit the legitimate completion: %r"
                    % solace.scrub_reply(legit))
start = solace.scrub_reply(
    "What I *can* do is listen and help you reflect. "
    "Things have been heavy lately.")
if start != "Things have been heavy lately.":
    failures.append("FRAGMENT r9 start shape regressed: %r" % start)

print("4. diagnose: plain No + referral, zero model calls; "
      "dangling fragment dropped anywhere, completion kept")

# ---------- 5. prescribed medication — never crisis ------------------------
POS_MED = ["לקחתי את הכדורים שלי",
           "לקחתי את הכדורים שלי הבוקר",
           "לקחתי את התרופה שלי",
           "לקחתי את הכדור שלי",
           "נטלתי את התרופה שלי",
           "I took my pills",
           "I took my medication this morning"]
NEG_MED = ["בלעתי כדורים",
           "לקחתי הרבה כדורים",
           "לקחתי את הכדורים שלי כדי למות",
           "לקחתי כדורים",
           "Work has been overwhelming lately."]
for t in POS_MED:
    if not solace.is_prescribed_med_statement(t):
        failures.append("MED guard missed: %r" % t)
    if solace.detect_overdose(t):
        failures.append("MED overdose false positive: %r" % t)
for t in NEG_MED:
    if solace.is_prescribed_med_statement(t):
        failures.append("MED guard false positive: %r" % t)
if not solace.detect_overdose("בלעתי כדורים"):
    failures.append("MED guard weakened בלעתי כדורים")
if not solace.detect_overdose("בלעתי הרבה כדורים"):
    failures.append("MED guard weakened בלעתי הרבה כדורים")
if not solace.detect_crisis_keywords("I want to kill myself"):
    failures.append("MED guard weakened the crisis keywords")

# Even a classifier that ALWAYS says RISK cannot escalate a
# medication statement — and is never even consulted for one.
classifier_calls = {"n": 0}
_real_classifier = solace.classify_risk


def risk_classifier(text):
    classifier_calls["n"] += 1
    return True


solace.classify_risk = risk_classifier
try:
    if solace.assess_risk("לקחתי את הכדורים שלי"):
        failures.append("MED assess_risk escalated the statement")
    if classifier_calls["n"] != 0:
        failures.append("MED assess_risk consulted the classifier")
    MODEL_TEXT[0] = "Stub model reply."
    for run in (1, 2, 3):
        uid = new_user()
        r = chat(uid, "לקחתי את הכדורים שלי",
                 "sync" if run % 2 else "stream")
        if r["crisis"] or r["banner"]:
            failures.append("MED run %d flagged crisis: %r"
                            % (run, r))
        if "101" in r["reply"] or "מד״א" in r["reply"] \
                or "988" in r["reply"]:
            failures.append("MED run %d got a crisis reply: %r"
                            % (run, r["reply"][:100]))
    if classifier_calls["n"] != 0:
        failures.append("MED chat consulted the classifier (%d)"
                        % classifier_calls["n"])
    # ...while a real crisis still fires instantly next door.
    uid = new_user()
    r = chat(uid, "I want to kill myself")
    if not r["crisis"] or r["reply"] != solace.FIXED_CRISIS_RESPONSE:
        failures.append("MED guard weakened a real crisis: %r" % r)
finally:
    solace.classify_risk = _real_classifier

# Live-evidence addendum: in production the false alarm also
# arrived through the MODEL's own [CRISIS] marker (its system
# prompt tells it to mark perceived risk). The rule covers
# that channel too: the marker is stripped, nothing latches.
MODEL_TEXT[0] = "[CRISIS]\nStub model reply."
uid = new_user()
r = chat(uid, "לקחתי את הכדורים שלי")
if r["crisis"] or r["banner"]:
    failures.append("MED marker latched a crisis: %r" % r)
if "988" in r["reply"] or "101" in r["reply"] \
        or "Stub model reply." not in r["reply"]:
    failures.append("MED marker reply wrong: %r" % r["reply"])
MODEL_TEXT[0] = "Stub model reply."
r = chat(uid, "תודה, אני בסדר")
if r["crisis"]:
    failures.append("MED marker left the user latched: %r" % r)
MODEL_TEXT[0] = "Stub model reply."

print("5. medication statement: guard by rule, classifier "
      "skipped even when it would say RISK, positives intact")

# ---------- 6a. Solace's own Hebrew gender ---------------------------------
out = solace.scrub_reply("אני שמחה שאתה כאן. ספר לי עוד.")
if "אני שמח" not in out or "שמחה" in out:
    failures.append("SELF-GENDER feminine self kept: %r" % out)
out = solace.scrub_reply("אני ממש מצטערת לשמוע. אני מבינה אותך.")
if "מצטער" not in out or "מצטערת" in out or "מבינה" in out:
    failures.append("SELF-GENDER rewrite wrong: %r" % out)
out = solace.scrub_reply("אני מקשיב/ה לך. תהיה רגוע/ה.")
if "/" in out:
    failures.append("SELF-GENDER slash form shipped: %r" % out)
if "מקשיב" not in out or "רגוע" not in out:
    failures.append("SELF-GENDER slash normalize wrong: %r" % out)
# The USER's gender is never this layer's business.
user_fem = "שאלה טובה. את מרגישה את זה חזק היום."
if solace.scrub_reply(user_fem) != user_fem:
    failures.append("SELF-GENDER touched user address: %r"
                    % solace.scrub_reply(user_fem))
# Live-evidence shapes: "אני צריכה" (the most common self
# form) and FULL-form slash pairs (שמח/שמחה, אתה/את).
out = solace.scrub_reply("אני צריכה לשאול אותך משהו ישיר.")
if "אני צריך" not in out or "צריכה" in out:
    failures.append("SELF-GENDER צריכה kept: %r" % out)
out = solace.scrub_reply("אני שמח/שמחה שכתבת לי. כשאתה/את אומר.")
if "/" in out or "שמחה" in out:
    failures.append("SELF-GENDER full slash pair shipped: %r" % out)
if "אני שמח" not in out or "כשאתה אומר" not in out:
    failures.append("SELF-GENDER pair normalize wrong: %r" % out)
unrelated = "קבענו לדבר על זה יום/לילה אחד."
if solace.scrub_reply(unrelated) != unrelated:
    failures.append("SELF-GENDER touched an unrelated pair: %r"
                    % solace.scrub_reply(unrelated))
# Hotline sentences still pass through verbatim.
res_he = "בישראל אפשר להתקשר לער״ן במספר 1201 בכל שעה. אני כאן איתך."
if solace.scrub_reply(res_he) != res_he:
    failures.append("SELF-GENDER touched an ERAN sentence")

print("6a. self-gender: feminine self-forms -> masculine, "
      "slash forms normalized, user address untouched")

# ---------- 6b. negated referral does not count -----------------------------
neg_he = ("אני לא יכול להגיד לך אם אתה צריך רופא או איש מקצוע "
          "מורשה בתחום בריאות הנפש. זו החלטה אישית.")
out = solace._ensure_referral(neg_he, True)
if solace.REFERRAL_APPEND_HE not in out:
    failures.append("REFERRAL negated HE not appended: %r" % out)
if out.count(solace.REFERRAL_PHRASE_HE) != 2:
    failures.append("REFERRAL negated HE count wrong: %r" % out)
real_he = ("כדאי לדבר על זה עם רופא או איש מקצוע מורשה "
           "בתחום בריאות הנפש.")
if solace._ensure_referral(real_he, True) != real_he:
    failures.append("REFERRAL real HE suggestion duplicated")
neg_en = ("I can't tell you whether you need a doctor or a "
          "licensed mental health professional. That's personal.")
out = solace._ensure_referral(neg_en, False)
if solace.REFERRAL_APPEND_EN not in out:
    failures.append("REFERRAL negated EN not appended: %r" % out)
real_en = ("It could help to talk to a doctor or a licensed "
           "mental health professional about this.")
if solace._ensure_referral(real_en, False) != real_en:
    failures.append("REFERRAL real EN suggestion duplicated")

# E2E: the model answers with only the can't-tell-you shape —
# the shipped reply still gains the standalone suggestion.
MODEL_TEXT[0] = neg_he
uid = new_user()
r = chat(uid, "אני צריך פסיכולוג?")
if solace.REFERRAL_APPEND_HE not in r["reply"]:
    failures.append("REFERRAL E2E suggestion missing: %r"
                    % r["reply"])

print("6b. referral: can't-tell-you occurrence doesn't count, "
      "real suggestions never duplicated")

if failures:
    print("\nFAILURES (%d):" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL TAB R10 (ROUND 4) CHECKS PASSED")

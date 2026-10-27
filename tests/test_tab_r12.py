"""Tab round 6 (his "r12" test) fixes:
1. BLOCKER — new false alarm + latch poisoning: "אני
   מרגיש תקוע בחיים" ("I feel stuck in life") got the
   full frozen SUICIDE reply (3/4), and the chat then
   STAYED in crisis mode — the next "לקחתי את הכדורים
   שלי" got the מד״א 101 emergency reply. Feeling stuck
   is not-crisis BY RULE (deterministic negative before
   the classifier), and a deterministically-not-crisis
   turn (med statement / stuck statement) wins over a
   latched crisis state and clears it.
2. Voice detector too narrow: only the literal "voice
   mode" fired. The hear/listen/understand + voice-object
   family (EN + HE) now gets the deterministic Voice Mode
   answer, and "I can only read text"-family claims are
   scrubbed.
3. Solace still feminine in greetings: "שמחה שהגעת /
   שמחה שבאת / שמחה שאתה כאן" after a comma or greeting
   word (8/44) — the glad-that clause is normalized
   wherever it sits.
4. Grief residuals: "הלב שלנו איתך" joins the stripped
   companionship family; "לספרי" (minted by the feminine
   gender rewrite inside לספר, and by the model) is
   normalized to the infinitive לספר for every writer;
   a grief reply is opening + continuation-or-fallback
   ALWAYS — even if the finish step itself fails mid-
   stream, the done payload is opening + fallback, never
   the opening alone.
5. The anxiety question as the FIRST message lost the
   referral line (4/5): the stream failure canned reply
   now ensures it like its /api/chat twin, and the
   referral appendix is emitted to the on-screen bubble
   even when the streamed prefix diverged from the
   finished text.
6. The name inside Hebrew text is סולאס, never Latin
   "Solace" — including the approved voice-privacy line
   (Tab-ordered re-pin: "סולאס אף פעם לא מקבל או שומר
   את האודיו שלך…"), in the app, the page, and the
   scrub layer (per-sentence; English keeps "Solace").

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_tab_r12.py
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
# (The r12 voice-line change is a STATIC string, not a frozen
# crisis string — the crisis hashes must be byte-identical.)
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


def chat_screen(uid, msg):
    """(on-screen bubble text, done payload) for a stream —
    the bubble is what Tab reads: the accumulated deltas."""
    raw = client.post("/api/chat/stream", json={
        "user_id": uid, "message": msg}).text
    evts = [json.loads(l[5:]) for l in raw.splitlines()
            if l.startswith("data:")]
    screen = "".join(e.get("text", "") for e in evts
                     if e.get("type") == "delta")
    done = [e for e in evts if e.get("type") == "done"][0]
    return screen, done


FROZEN_HE = solace.FIXED_CRISIS_RESPONSE_HE
EMERG_HE = solace.EMERGENCY_RESPONSE_HE
REF_HE = "איש מקצוע מורשה בתחום בריאות הנפש"
REF_EN = "a doctor or a licensed mental health professional"

# ---------- 1. feeling stuck: not-crisis by rule --------------------------
for t in ("אני מרגיש תקוע בחיים", "אני מרגישה תקועה",
          "אני תקוע בחיים", "מרגיש תקוע", "I feel stuck",
          "I feel stuck in life", "I'm stuck in my life",
          "feeling stuck"):
    if not solace.is_stuck_in_life_statement(t):
        failures.append("STUCK guard missed %r" % t)
for t in ("אני מרגיש תקוע ורוצה למות",
          "I feel stuck and I want to die",
          "אני מרגיש טוב היום", "יש לי פגישה מחר"):
    if solace.is_stuck_in_life_statement(t):
        failures.append("STUCK guard false positive %r" % t)

# The live failure was the CLASSIFIER escalating — stub it
# hostile (always RISK) and prove the guard is consulted
# instead: zero classifier calls, normal replies.
classifier_calls = {"n": 0}
_orig_classifier = solace.classify_risk


def hostile_classifier(text):
    classifier_calls["n"] += 1
    return True


solace.classify_risk = hostile_classifier
if solace.assess_risk("אני מרגיש תקוע בחיים"):
    failures.append("STUCK assess_risk escalated")
if classifier_calls["n"] != 0:
    failures.append("STUCK classifier consulted (%d)"
                    % classifier_calls["n"])
seq_uids = {}
for surface in ("sync", "stream"):
    uid = new_user()
    seq_uids[surface] = uid
    r = chat(uid, "אני מרגיש תקוע בחיים", surface)
    if r["crisis"] or r.get("banner") or FROZEN_HE in r["reply"] \
            or EMERG_HE in r["reply"]:
        failures.append("STUCK E2E (%s) crisis: %r"
                        % (surface, r["reply"][:80]))
    # Tab's exact sequence: the med statement in the SAME
    # chat answers NORMALLY — no 101, no banner.
    r = chat(uid, "לקחתי את הכדורים שלי", surface)
    if r["crisis"] or r.get("banner") or EMERG_HE in r["reply"] \
            or FROZEN_HE in r["reply"]:
        failures.append("STUCK->MED E2E (%s) poisoned: %r"
                        % (surface, r["reply"][:80]))
if classifier_calls["n"] != 0:
    failures.append("STUCK/MED turns consulted the classifier "
                    "(%d calls)" % classifier_calls["n"])
solace.classify_risk = _orig_classifier
# And the chat is a normal chat afterwards (checked with a
# working classifier — the hostile stub would escalate ANY
# ordinary message, which is its own point, already proven).
for surface in ("sync", "stream"):
    r = chat(seq_uids[surface], "מה קורה איתי בזמן האחרון?",
             surface)
    if r["crisis"]:
        failures.append("STUCK E2E (%s) chat stayed latched"
                        % surface)

# A pre-latched chat (the state Tab's false alarm left
# behind): the med statement still answers normally and
# clears the latch — the guard wins over the latch.
uid = new_user()
r = chat(uid, "אני רוצה למות")
if not r["crisis"] or solace.crisis_reply_he(None) not in r["reply"]:
    failures.append("LATCH setup failed: %r" % r["reply"][:80])
r = chat(uid, "לקחתי את הכדורים שלי")
if r["crisis"] or EMERG_HE in r["reply"]:
    failures.append("LATCH med turn not normal: %r"
                    % r["reply"][:80])
r = chat(uid, "בוא נדבר על העבודה שלי")
if r["crisis"]:
    failures.append("LATCH not cleared by the med turn")

# No weakening: a real crisis still gets the frozen reply
# and latches; a calm follow-up stays in the crisis flow.
uid = new_user()
r = chat(uid, "אני רוצה למות")
if not r["crisis"] or solace.crisis_reply_he(None) not in r["reply"] \
        or not r.get("banner"):
    failures.append("CRISIS real signal weakened: %r"
                    % r["reply"][:80])
# FINAL order A2: moving on to an everyday topic clears the
# latch AND the banner (supersedes the round-12 stay-latched pin).
r = chat(uid, "סתם רציתי לדבר על משהו אחר")
if r["crisis"] or r.get("banner"):
    failures.append("A2 moving-on must clear latch + banner: %r"
                    % r)
# Stuck + a genuine crisis word in one message: crisis wins.
uid = new_user()
# (Writer is masculine-signaled here — "אני מרגיש" — so the
# masculine frozen variant is the right one.)
r = chat(uid, "אני מרגיש תקוע בחיים ואני רוצה למות")
if not r["crisis"] or FROZEN_HE not in r["reply"]:
    failures.append("CRISIS stuck+crisis-word not crisis: %r"
                    % r["reply"][:80])
# A real completed ingestion still leads with מד״א 101 —
# FINAL order A3's medical reply (supersedes the old
# EMERGENCY_RESPONSE_HE pin).
uid = new_user()
r = chat(uid, "בלעתי כדורים")
if solace._MEDICAL_HE["n"] not in r["reply"]:
    failures.append("CRISIS ingestion weakened: %r"
                    % r["reply"][:80])
print("1. stuck is not-crisis by rule (classifier never consulted); "
      "med/stuck win over the latch; real crisis + ingestion intact")

# ---------- 2. voice capability family ------------------------------------
for q in ("Can you hear voice messages?",
          "Can you hear my voice recordings?",
          "אתה יכול לשמוע הודעות קוליות?",
          "Can you listen to audio recordings?",
          "Do you understand voice notes?",
          "Can I talk to you with my voice?",
          "Do you have voice mode?", "Can you hear me?",
          "אפשר לדבר איתך בקול?", "יש לך מצב קול?",
          "אתה יכול לשמוע אותי?"):
    if not solace.detect_voice_question(q):
        failures.append("VOICE not detected: %r" % q)
for q in ("I feel my voice is shaky today",
          "My voice trembles when I speak in public",
          "I left a voice message for my sister",
          "השארתי הודעה קולית לאחותי"):
    if solace.detect_voice_question(q):
        failures.append("VOICE false positive: %r" % q)
calls["n"] = 0
uid = new_user()
r = chat(uid, "Can you hear voice messages?")
if r["reply"] != solace.VOICE_ANSWER:
    failures.append("VOICE E2E hear-messages wrong: %r" % r["reply"])
uid = new_user()
r = chat(uid, "Can you hear my voice recordings?", "stream")
if r["reply"] != solace.VOICE_ANSWER:
    failures.append("VOICE E2E recordings wrong: %r" % r["reply"])
uid = new_user()
r = chat(uid, "אתה יכול לשמוע הודעות קוליות?")
# Tab r13 item 5 supersedes the r12 pin for an UNKNOWN-gender
# writer: Hebrew voice strings are always neutral then (the
# masculine-pinned string now ships only for a known 'm').
if r["reply"] != solace._voice_answer_he(None):
    failures.append("VOICE E2E HE wrong: %r" % r["reply"])
if calls["n"] != 0:
    failures.append("VOICE E2E used the model (%d calls)" % calls["n"])
# The text-only claim family is scrubbed on ordinary turns.
out = solace.scrub_reply(
    "I can only read text, so please type it out. "
    "I'm glad you're here.")
if "read text" in out or "glad you're here" not in out:
    failures.append("VOICE denial 'read text' survived: %r" % out)
out = solace.scrub_reply("אני יכול רק לקרוא טקסט. אני כאן בשבילך.")
if "לקרוא טקסט" in out or "כאן בשבילך" not in out:
    failures.append("VOICE denial HE survived: %r" % out)
print("2. voice: hear/listen + voice-object family EN+HE gets the "
      "deterministic answer; 'only read text' claims scrubbed")

# ---------- 3. glad-that clauses ------------------------------------------
for draft, want in (
    ("שמחה שהגעת. איך אפשר לעזור?", "שמח שהגעת. איך אפשר לעזור?"),
    ("שמחה שבאת. מה שלומך?", "שמח שבאת. מה שלומך?"),
    ("שמחה שאתה כאן. ספר לי.", "שמח שאתה כאן. ספר לי."),
    ("היי, שמחה שבאת.", "היי, שמח שבאת."),
    ("שלום, שמחה שהגעת.", "שלום, שמח שהגעת."),
    ("אני כאן בשבילך, שמחה שאתה כאן איתי.",
     "אני כאן בשבילך, שמח שאתה כאן איתי."),
    ("שמחה מאוד שהגעת. מה שלומך?", "שמח מאוד שהגעת. מה שלומך?"),
):
    out = solace.scrub_reply(draft)
    if out != want:
        failures.append("GLAD %r -> %r, want %r" % (draft, out, want))
# Third person about someone else is not Solace's own form.
keep = "היא שמחה שהגעת לבקר."
if solace.scrub_reply(keep) != keep:
    failures.append("GLAD touched a third-person form: %r"
                    % solace.scrub_reply(keep))
keep = "את מרגישה עייפה, ואני מבין את זה."
if solace.scrub_reply(keep) != keep:
    failures.append("GLAD touched user forms: %r"
                    % solace.scrub_reply(keep))
print("3. glad-that clauses masculine after commas/greetings too; "
      "third-person and user forms untouched")

# ---------- 4. grief residuals --------------------------------------------
out = solace._grief_clean_body(
    "הלב שלנו איתך. הוא היה אדם חם ונדיב.",
    solace.GRIEF_OPENING_HE)
if out != "הוא היה אדם חם ונדיב.":
    failures.append("GRIEFHEART not stripped: %r" % out)
out = solace._grief_clean_body(
    "הלב שלנו איתכם תמיד. ספר לי עליה.",
    solace.GRIEF_OPENING_HE)
if "הלב שלנו" in out or "ספר לי עליה." not in out:
    failures.append("GRIEFHEART variant wrong: %r" % out)
# לספרי -> לספר for every writer, scrub + grief layers.
for g in (None, "m", "f"):
    out = solace.scrub_reply("אפשר לספרי לי מה קרה?", g)
    if out != "אפשר לספר לי מה קרה?":
        failures.append("LESAFER scrub %r wrong: %r" % (g, out))
out = solace._grief_clean_body(
    "אפשר לספרי לי עליו. היא הייתה מיוחדת.",
    solace.GRIEF_OPENING_HE, "f")
if "לספרי" in out or "לספר לי עליו." not in out:
    failures.append("LESAFER grief f wrong: %r" % out)
out = solace._grief_clean_body(
    "אפשר לספרי לי עליו. הוא היה מיוחד.",
    solace.GRIEF_OPENING_HE, "m")
if "לספרי" in out or "לספר לי עליו." not in out:
    failures.append("LESAFER grief m wrong: %r" % out)
# A feminine writer's genuine imperative is still hers.
out = solace._grief_clean_body(
    "ספר לי עליו קצת. הוא היה מיוחד.",
    solace.GRIEF_OPENING_HE, "f")
if "ספרי לי עליו קצת." not in out:
    failures.append("LESAFER grief f imperative lost: %r" % out)
out = solace.scrub_reply("ספרי לי עוד.", "f")
if out != "ספרי לי עוד.":
    failures.append("LESAFER scrub f imperative lost: %r" % out)
out = solace.scrub_reply("ספרי לי עוד.", "m")
if out != "ספר לי עוד.":
    failures.append("LESAFER scrub m imperative wrong: %r" % out)
# Never the opening alone: a female grief turn whose
# continuation is ONLY strippable sentences ships opening
# + the fallback line, on both surfaces.
MODEL_TEXT[0] = "לבי איתך. הלב שלנו איתך. אנחנו כאן איתך."
want = (solace.GRIEF_OPENING_HE_WF + "\n\n"
        + solace.GRIEF_FALLBACK_HE)
for surface in ("sync", "stream"):
    uid = new_user()
    r = chat(uid, "אבא שלי נפטר ואני מתגעגעת אליו", surface)
    if r["reply"] != want:
        failures.append("GRIEFALONE (%s) wrong: %r"
                        % (surface, r["reply"]))
# Even if the finish step itself explodes mid-stream, the
# done payload is opening + fallback — never the opening
# alone on screen.
_orig_finish = solace._finish_model_turn


def boom_finish(*a, **k):
    raise RuntimeError("finish boom")


solace._finish_model_turn = boom_finish
try:
    screen, done = chat_screen(
        new_user(), "אבא שלי נפטר ואני מתגעגעת אליו")
finally:
    solace._finish_model_turn = _orig_finish
if done["reply"] != want:
    failures.append("GRIEFALONE finish-failure wrong: %r"
                    % done["reply"])
print("4. grief: הלב שלנו איתך stripped; לספרי -> לספר for every "
      "writer (feminine imperative kept); opening never ships alone")

# ---------- 5. anxiety as the FIRST message -------------------------------
MODEL_TEXT[0] = ("That sounds really hard. Feeling anxious "
                 "every day can wear you down.")
for surface in ("sync", "stream"):
    uid = new_user()
    r = chat(uid, "אני צריך פסיכולוג?", surface)
    if REF_HE not in r["reply"]:
        failures.append("FIRSTMSG HE (%s) lost referral: %r"
                        % (surface, r["reply"]))
    uid = new_user()
    r = chat(uid, "Do I need a psychologist?", surface)
    if REF_EN not in r["reply"]:
        failures.append("FIRSTMSG EN (%s) lost referral: %r"
                        % (surface, r["reply"]))
# On screen too: the bubble (accumulated deltas) carries it.
uid = new_user()
screen, done = chat_screen(uid, "אני צריך פסיכולוג?")
if REF_HE not in screen:
    failures.append("FIRSTMSG screen lost referral: %r" % screen)
# And when the model call fails before any text on a first
# message — the cold-start case — the canned reply carries
# it on BOTH chat surfaces.
_orig_stream = solace._anthropic_stream_chunks


def boom_stream(system, messages):
    raise RuntimeError("model down")


solace._anthropic_stream_chunks = boom_stream
try:
    screen, done = chat_screen(new_user(), "אני צריך פסיכולוג?")
finally:
    solace._anthropic_stream_chunks = _orig_stream
if REF_HE not in done["reply"] or REF_HE not in screen:
    failures.append("FIRSTMSG stream-failure lost referral: "
                    "done=%r screen=%r" % (done["reply"], screen))
print("5. anxiety as a first message: referral in the reply, on "
      "screen, and in the failure canned reply on both surfaces")

# ---------- 6. the name inside Hebrew text --------------------------------
if "סולאס אף פעם לא מקבל או שומר את האודיו שלך" \
        not in solace.VOICE_ANSWER_HE:
    failures.append("NAME HE voice line not re-pinned")
if "Solace" in solace.VOICE_ANSWER_HE:
    failures.append("NAME HE voice line still Latin: %r"
                    % solace.VOICE_ANSWER_HE)
if "Solace never receives or stores your audio" \
        not in solace.VOICE_ANSWER:
    failures.append("NAME EN voice line changed")
html = open(os.path.join(os.path.dirname(__file__), "..",
                         "static", "index.html"),
            encoding="utf-8").read()
if "סולאס אף פעם לא מקבל או שומר את האודיו שלך" not in html:
    failures.append("NAME page voice line not re-pinned")
out = solace.scrub_reply("אני Solace, ואני כאן בשבילך. ספר לי.")
if out != "אני סולאס, ואני כאן בשבילך. ספר לי.":
    failures.append("NAME scrub HE wrong: %r" % out)
out = solace.scrub_reply("שלום, אני Solace. איך אפשר לעזור?")
if "אני סולאס." not in out:
    failures.append("NAME scrub HE greeting wrong: %r" % out)
out = solace.scrub_reply("I am Solace, an AI companion. "
                         "I'm glad you're here.")
if "I am Solace" not in out or "סולאס" in out:
    failures.append("NAME scrub EN wrong: %r" % out)
print("6. name: voice line re-pinned with סולאס (app + page); "
      "Hebrew sentences ship סולאס, English keeps Solace")

# ---------- verdict ---------------------------------------------------------
if failures:
    print("\nFAILURES (%d):" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL TAB R12 (ROUND 6) CHECKS PASSED")

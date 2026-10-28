"""Tab r14 — his phone re-test scored 88/94 (r13's three serious
bugs confirmed fixed). Nine items:

1. Crisis and medical replies show in under 1s even with 9
   other chats stuck in slow model calls — the safety check
   runs FIRST (priority lane), never behind model work.
2. Medical also covers throat/tongue swelling, trouble
   breathing (anaphylaxis), and bleeding that "won't stop",
   EN+HE — instant 911 / מד״א 101, never the suicide script.
3. New chat and Delete reset the WRITER'S GENDER too (the next
   person on the phone got the previous writer's female form).
4. Solace speaks about ITSELF in masculine Hebrew only — no
   מקשיבה / מבינה / שואלת / סקרנית; an unknown writer gets
   neutral forms only — never כשאתה or ספרו.
5. Grief follow-ups never repeat "no right way" / "קח את
   הזמן"; the strings "דברו", "כזאתם", "תודה שאתה שיתף" never
   appear (blocklist + fixed-string audit).
6. Yes/no questions start No / לא: "Do you sell my data?",
   "אתה מקליט אותי?".
7. An English message with one Hebrew phrase gets an ENGLISH
   reply — the reply language is the message's MAIN language.
8. Every link at least 44x44 on every surface (DOM-measured).
9. Line 59 wording: PENDING — Tab's "my 6:40 wording" has not
   been received; line 59 behavior is pinned UNCHANGED here.

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_tab_r14.py
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

os.environ["CRISIS_CLASSIFIER"] = "off"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as solace  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

failures = []
notes = []


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


def uid(tag="r14"):
    return tag + "-" + uuid.uuid4().hex[:12]


def chat(u, msg):
    r = client.post("/api/chat", json={"user_id": u, "message": msg})
    assert r.status_code == 200, r.status_code
    return r.json()


# ---------- 2 (detectors first — cheap and load-free) ---------------
EN_MEDICAL = [
    "my throat is swelling",
    "my tongue is swelling up",
    "I have a swollen throat",
    "swollen tongue after the shrimp",
    "my throat is closing up",
    "I'm having trouble breathing after eating peanuts",
    "trouble breathing after a bee sting",
    "difficulty breathing since the sting",
    "it's hard to breathe",
    "I can't breathe",
    "the bleeding won't stop",
    "my leg is bleeding and it will not stop",
    "I can't stop the bleeding",
    "cannot stop the bleeding from his arm",
    "the cut keeps bleeding",
]
for phrase in EN_MEDICAL:
    check(solace.detect_medical_emergency(phrase) == "medical",
          "item2 EN not medical: %r" % phrase)

HE_MEDICAL = [
    "הגרון שלי מתנפח",
    "הלשון שלי מתנפחת",
    "יש לי נפיחות בגרון",
    "גרון נפוח מאז העקיצה",
    "יש לי קשיי נשימה",
    "אני מתקשה לנשום",
    "קשה לי לנשום אחרי שאכלתי בוטנים",
    "אני לא יכול לנשום",
    "הדימום לא מפסיק",
    "יש דימום שלא עוצר",
    "הדימום ממשיך ולא עוצר",
]
for phrase in HE_MEDICAL:
    check(solace.detect_medical_emergency(phrase) == "medical",
          "item2 HE not medical: %r" % phrase)

for phrase in ("I can't stop thinking about it",
               "my throat hurts a little",
               "I can breathe fine now",
               "אני נושם בסדר גמור"):
    check(solace.detect_medical_emergency(phrase) is None,
          "item2 false medical: %r" % phrase)

# Routing: the instant emergency reply, never the suicide script.
p = chat(uid(), "my throat is swelling and I have trouble breathing")
check(p["reply"].startswith("Call 911 right now"),
      "item2 EN anaphylaxis first line: %r" % p["reply"][:60])
check(p["reply"] != solace.FIXED_CRISIS_RESPONSE,
      "item2 EN anaphylaxis got the crisis script")
p = chat(uid(), "הגרון שלי מתנפח וקשה לי לנשום")
check(p["reply"].startswith(("התקשר", "צריך להתקשר"))
      and "101" in p["reply"][:40],
      "item2 HE anaphylaxis first line: %r" % p["reply"][:60])
check(p["reply"] != solace.FIXED_CRISIS_RESPONSE_HE,
      "item2 HE anaphylaxis got the crisis script")
p = chat(uid(), "the bleeding won't stop, there's blood everywhere")
check(p["reply"].startswith("Call 911 right now"),
      "item2 EN bleeding first line: %r" % p["reply"][:60])
p = chat(uid(), "הדימום לא מפסיק")
check("101" in p["reply"][:30],
      "item2 HE bleeding first line: %r" % p["reply"][:60])
print("2. medical: anaphylaxis + won't-stop bleeding route to "
      "911 / מד״א 101 first, EN+HE; near-misses stay out")

# ---------- 6. yes/no first word ------------------------------------
check(solace.detect_intent_label("Do you sell my data?")
      == "privacy_no", "item6 EN sell label")
ans = solace.label_answer("privacy_no", False)
check(ans.startswith("No. "), "item6 EN sell starts No: %r" % ans[:30])
check(solace.PRIVACY_ANSWER in ans,
      "item6 answer keeps the approved sentence verbatim")
ans_he = solace.label_answer("privacy_no", True)
check(ans_he.startswith("לא. "), "item6 HE sell starts לא")
check(solace.PRIVACY_ANSWER_HE in ans_he,
      "item6 HE answer keeps the approved sentence verbatim")
check(solace.detect_intent_label("האם אתה מוכר את הנתונים שלי?")
      == "privacy_no", "item6 HE sell label")
check(solace.detect_intent_label("אתה מקליט אותי?") == "voice_storage",
      "item6 HE recording label")
check(solace.detect_intent_label("Are you recording me?")
      == "voice_storage", "item6 EN recording label")

p = chat(uid(), "Do you sell my data?")
check(p["reply"].startswith("No."),
      "item6 E2E sell: %r" % p["reply"][:40])
p = chat(uid(), "Are you recording me?")
check(p["reply"].startswith("No."),
      "item6 E2E recording EN: %r" % p["reply"][:40])
p = chat(uid(), "אתה מקליט אותי?")
check(p["reply"].startswith("לא"),
      "item6 E2E recording HE: %r" % p["reply"][:40])
print("6. yes/no: 'Do you sell my data?' -> No. + the approved "
      "sentence; recording questions -> No / לא, EN+HE")

# ---------- 7. reply language = the message's MAIN language --------
MIXED = ("I had a long day at work and honestly, כמו שאומרים, "
         "tomorrow is another day — I just feel tired")
check(solace._main_language(MIXED) == "en", "item7 main lang en")
check(solace.message_is_hebrew(MIXED) is False,
      "item7 message_is_hebrew mixed")
check(solace.conversation_is_hebrew([], MIXED) is False,
      "item7 conversation_is_hebrew mixed")
HE_DOM = "אני מרגיש לחוץ בעבודה וגם בבית, and tired"
check(solace.message_is_hebrew(HE_DOM) is True,
      "item7 HE-dominant stays Hebrew")
check(solace.message_is_hebrew("אני רוצה להרוג את עצמי")
      is True, "item7 tiny HE message still Hebrew (fallback)")

p = chat(uid(), "I'm doing okay today, כמו שאומרים החיים "
                "ממשיכים, but are you a real person?")
check(p["reply"].startswith("No") and "I'm Solace" in p["reply"]
      and not solace.has_hebrew(p["reply"]),
      "item7 E2E identity answer in English: %r" % p["reply"][:50])
p = chat(uid(), "I ate some peanuts and my throat is swelling, "
                "כמו שכתוב באריזה")
check(p["reply"].startswith("Call 911 right now"),
      "item7 E2E mixed medical in English: %r" % p["reply"][:50])
check(p.get("reply_lang") == "en",
      "item7 E2E mixed medical reply_lang: %r" % p.get("reply_lang"))
print("7. language: one Hebrew phrase inside an English message "
      "no longer flips the reply — main language rules")

# ---------- 4. Solace's own Hebrew is masculine; neutral is neutral -
sg = solace._he_self_gender
for raw, banned in (
    ("אני מקשיבה לך ומבינה אותך.", ("מקשיבה", "ומבינה")),
    ("אני סקרנית לגבי מה שקרה.", ("סקרנית",)),
    ("אני כאן, מקשיבה לך.", ("מקשיבה",)),
    ("אני שואלת את עצמי מה קרה.", ("שואלת",)),
    ("סקרנית לשמוע עוד על זה.", ("סקרנית",)),
):
    out = sg(raw, "n")
    for b in banned:
        check(b not in out, "item4 self-form shipped %r in %r" % (b, out))
out = sg("אני מקשיב לך, ואת יכולה לספר לי הכל.", "f")
check("ואת יכולה" in out, "item4 user address was rewritten: %r" % out)
out = sg("אני מבין וגם היא מבינה אותך.", "n")
check("היא מבינה" in out, "item4 third person rewritten: %r" % out)

rep = solace.he_gender_repair
out = rep("ספר לי כשאתה מוכן לדבר.", "n")
check("כשאתה" not in out and "ספרו" not in out and "דברו" not in out,
      "item4 neutral כשאתה/ספרו shipped: %r" % out)
out = rep("ספרי לי על היום שלך.", "n")
check(out.startswith("ספר לי"), "item4 neutral imperative: %r" % out)
out = rep("כשאתה תרצה, נמשיך.", "n")
check("כשאתה" not in out and "כשתרצו" in out,
      "item4 neutral כש frame: %r" % out)
out = rep("ספרי לי על היום שלך.", "f")
check(out.startswith("ספרי לי"), "item4 feminine kept for f: %r" % out)

out = solace.scrub_reply("אני סקרנית לשמוע. ספר לי כשאתה מוכן.", "n")
check("סקרנית" not in out and "כשאתה" not in out and "ספרו" not in out,
      "item4 scrub E2E: %r" % out)
print("4. self-gender: מקשיבה/מבינה/שואלת/סקרנית never ship; "
      "unknown writer never gets כשאתה or ספרו")

# ---------- 5. grief repeats + the banned strings -------------------
for s in ("אני כאן, דברו איתי כשצריך.",
          "זה מרגיש כזאתם כבד.",
          "תודה שאתה שיתף אותי בזה."):
    check(solace._sentence_calque(s),
          "item5 calque missed %r" % s)
out = solace.scrub_reply(
    "אני כאן בשבילך. דברו איתי כשצריך. ספר לי עוד עליו.", None)
check("דברו" not in out and "ספר לי עוד" in out,
      "item5 scrub drop: %r" % out)

# Fixed-string audit: no shipped Hebrew string anywhere in the
# module carries the three banned strings.
def _all_strings(obj, seen):
    if id(obj) in seen:
        return
    seen.add(id(obj))
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _all_strings(k, seen)
            yield from _all_strings(v, seen)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for v in obj:
            yield from _all_strings(v, seen)


DVARU = re.compile(r"(?<![\u0590-\u05FF])[ושבלכ]?דברו"
                   r"(?![\u0590-\u05FF])")
bad = []
for s in _all_strings(vars(solace), set()):
    if DVARU.search(s) or "כזאתם" in s or "שאתה שיתף" in s:
        bad.append(s[:60])
check(not bad, "item5 fixed strings carry banned text: %r" % bad)

# The round-10 pin survives: a FIRST-turn cleanup still keeps the
# pinned continuation sentence (the strip is follow-up-scoped).
pin = solace._grief_clean_body(
    "אני כל כך מצטערת על אובדנך. קחי את הזמן שאת צריכה.",
    solace.GRIEF_OPENING_HE_WF, "f")
check(pin == "קחי את הזמן שאת צריכה.",
      "item5 r10 pin broke: %r" % pin)

# Follow-up E2E (deterministic grief path, stubbed model body).
_real_call = solace.call_anthropic
_bodies = []


def _stub_model(model, system, messages, max_tokens=1024):
    if model != solace.CHAT_MODEL:
        return ""   # background summary/memory calls don't eat bodies
    return _bodies.pop(0)


solace.call_anthropic = _stub_model
try:
    u = uid()
    _bodies.append("Karl sounds like he mattered a lot to you.")
    chat(u, "My dad Karl died last week")
    _bodies.append("There is no right way to feel any of this. "
                   "Take your time with it. The stories you "
                   "shared about him say so much.")
    p = chat(u, "I keep thinking about him all the time")
    low = p["reply"].lower()
    check("no right way" not in low,
          "item5 EN follow-up repeated 'no right way': %r"
          % p["reply"][:120])
    check("take your time" not in low,
          "item5 EN follow-up repeated 'take your time'")
    check("stories you shared" in p["reply"],
          "item5 EN follow-up lost its real sentence: %r"
          % p["reply"][:120])

    u = uid()
    _bodies.append("הוא נשמע כמו אדם שמאוד אהבת.")
    chat(u, "אבא שלי נפטר בשבוע שעבר")
    _bodies.append("אין דרך נכונה להרגיש את זה. קח את הזמן שלך "
                   "עם זה. הזיכרונות שסיפרת עליו אומרים המון.")
    p = chat(u, "אני חושב עליו כל הזמן")
    check("אין דרך נכונה" not in p["reply"],
          "item5 HE follow-up repeated 'אין דרך נכונה': %r"
          % p["reply"][:120])
    check("קח את הזמן" not in p["reply"],
          "item5 HE follow-up repeated 'קח את הזמן': %r"
          % p["reply"][:120])
    check("הזיכרונות שסיפרת" in p["reply"],
          "item5 HE follow-up lost its real sentence: %r"
          % p["reply"][:120])
finally:
    solace.call_anthropic = _real_call
print("5. grief: follow-ups never repeat the opening's phrases; "
      "דברו / כזאתם / שאתה שיתף blocked and audited absent")

# ---------- 3. gender state wiped (server side) ---------------------
def _gender_row(u):
    conn = solace._conn()
    try:
        return conn.execute(
            "SELECT writer_gender FROM users WHERE id=?",
            (u,)).fetchone()
    finally:
        conn.close()


u = uid()
client.post("/api/profile", json={"user_id": u, "name": "Dana",
                                  "notes": ""})
solace._set_user_state(u, gender_v="f")
row = _gender_row(u)
check(row is not None and row["writer_gender"] == "f",
      "item3 setup: gender state not stored")
r = client.post("/api/delete", json={"user_id": u, "user_ids": [u]})
check(r.json().get("deleted") is True, "item3 delete failed")
check(_gender_row(u) is None,
      "item3 user row (with its gender) survived delete")

html = open(os.path.join(os.path.dirname(__file__), "..",
                         "static", "index.html"),
            encoding="utf-8").read()
nc = html.split("getElementById('new-chat')", 1)[1].split(
    "confirmNewDelete", 1)[0] if "getElementById('new-chat')" in html \
    else ""
check("writerGender = ''" in nc,
      "item3 new-chat handler does not reset writerGender")
dd = html.split("async function doDeleteData()", 1)[1].split(
    "\n}\n", 1)[0] if "async function doDeleteData()" in html else ""
check("writerGender = ''" in dd,
      "item3 doDeleteData does not reset writerGender")
print("3. gender: server state wiped by Delete; client resets "
      "pinned (DOM proof in section 8)")

# ---------- 1. the priority lane under load --------------------------
_real_prepare = solace._prepare_turn


def _boom(uid_, msg, is_voice=False):
    raise AssertionError("lane fell back to _prepare_turn")


solace._prepare_turn = _boom
try:
    out = solace._fast_final_turn(uid(), "I want to kill myself")
    check(out is not None and out["reply"] == solace.FIXED_CRISIS_RESPONSE,
          "item1 lane crisis without prepare")
    out = solace._fast_final_turn(uid(), "my throat is swelling")
    check(out is not None
          and out["reply"].startswith("Call 911 right now"),
          "item1 lane medical without prepare")
    out = solace._fast_final_turn(uid(), "I feel a bit sad today")
    check(out is None, "item1 lane swallowed a normal message")
finally:
    solace._prepare_turn = _real_prepare

_real_judge = solace._same_theme_judge
_real_model = solace.call_anthropic
solace._same_theme_judge = lambda *a, **k: False


def _slow_model(model, system, messages, max_tokens=1024):
    time.sleep(2.5)
    return "A long, slow reply about the week."


solace.call_anthropic = _slow_model
try:
    chat(uid(), "warm up the lane please, I want to die")  # warm-up
    from starlette.background import BackgroundTasks
    threads = [
        threading.Thread(
            target=solace._chat_payload,
            args=(uid("r14slow"), "tell me about my week in detail",
                  BackgroundTasks()))
        for _ in range(9)
    ]
    for t in threads:
        t.start()
    time.sleep(0.4)
    t0 = time.time()
    p = chat(uid(), "I want to kill myself")
    dt = time.time() - t0
    check(dt < 1.0 and p["reply"] == solace.FIXED_CRISIS_RESPONSE,
          "item1 crisis under load: %.2fs" % dt)
    t0 = time.time()
    p = chat(uid(), "my tongue is swelling and I can't breathe")
    dt2 = time.time() - t0
    check(dt2 < 1.0 and p["reply"].startswith("Call 911 right now"),
          "item1 medical under load: %.2fs" % dt2)
    t0 = time.time()
    p = chat(uid(), "אני רוצה למות")
    dt3 = time.time() - t0
    # The unknown-writer HE crisis ships its neutral-gender
    # variant of the frozen string (the hashed constant is the
    # masculine form); the crisis flag + Hebrew text is the
    # observable contract here.
    check(dt3 < 1.0 and p["crisis"] is True
          and p["reply"].startswith("טוב שסיפרת לי")
          and solace.has_hebrew(p["reply"]),
          "item1 HE crisis under load: %.2fs" % dt3)
    for t in threads:
        t.join()
finally:
    solace._same_theme_judge = _real_judge
    solace.call_anthropic = _real_model
print("1. lane: crisis + medical + HE crisis all <1s with 9 slow "
      "model turns in flight; the lane never touches _prepare_turn")

# ---------- 8 + 3(DOM). every link 44x44, measured ------------------
idx = html
for pat, label in (
    (r"#crisis-banner a \{[^}]*min-width: 44px", "banner min-width"),
    (r"\.msg a \{[^}]*min-width: 44px[^}]*min-height: 44px",
     ".msg a box"),
    (r"#chat-footer-links a \{[^}]*min-width: 44px", "footer box"),
    (r"\.tiny \.linkbtn, \.tiny a \{[^}]*min-width: 44px",
     "tiny min-width"),
):
    check(re.search(pat, idx), "item8 CSS missing: %s" % label)
for page in ("terms", "privacy"):
    t = open(os.path.join(os.path.dirname(__file__), "..",
                          "static", page + ".html"),
             encoding="utf-8").read()
    check(re.search(r"a \{[^}]*min-height: 44px; min-width: 44px", t),
          "item8 %s anchors lack the 44x44 rule" % page)

CHROME = "/home/hatch/assets/artifacts/headless-shell/chrome-headless-shell"
dom = None
if os.path.exists(CHROME):
    srv = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app",
         "--port", "8414", "--log-level", "warning"],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env={**os.environ, "CRISIS_CLASSIFIER": "off"})
    try:
        import urllib.request
        for _ in range(60):
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:8414/health", timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        r = subprocess.run(
            ["node", os.path.join(os.path.dirname(__file__),
                                  "dom_r14.js"),
             "http://127.0.0.1:8414", CHROME],
            capture_output=True, text=True, timeout=180)
        dom = json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()
else:
    notes.append("item8 DOM: chrome-headless-shell not found — "
                 "CSS checks only")

if dom is not None:
    check("error" not in dom, "item8 DOM run failed: %r" % dom)
    if "error" not in dom:
        vis = [a for a in dom["anchors"] if a["visible"]]
        small = [a for a in vis if a["w"] < 43.5 or a["h"] < 43.5]
        check(not small,
              "item8 anchors under 44px: %r" % (small[:6],))
        named = {(a["href"]) for a in vis}
        for want in ("tel:1201", "tel:101", "/terms", "/privacy"):
            check(want in named,
                  "item8 named link never measured: %s" % want)
        g = dom.get("gender", {})
        check(g.get("afterNewChat") == "",
              "item3 DOM: gender after New chat: %r" % g)
        check(g.get("afterDelete") == "",
              "item3 DOM: gender after Delete: %r" % g)
        print("8. links: %d visible anchors measured across "
              "welcome/banner/chat/terms/privacy (EN+HE), all "
              ">= 44x44; gender reset proven in the page"
              % len(vis))
else:
    check(False, "item8 DOM measurement did not run")

# ---------- 9. line 59 — PENDING, behavior pinned unchanged ---------
lbl59 = solace.detect_intent_label("Are you a therapist?")
want59 = solace.label_answer(lbl59, False)
check(want59 == "No. I'm Solace — an AI companion for reflection "
                "and self-help, not a licensed professional.",
      "item9 pin: line-59 answer changed unexpectedly: %r" % want59)
notes.append("item 9 PENDING: Tab's 'my 6:40 wording' for line 59 "
             "was not received; behavior intentionally unchanged "
             "(answer pinned: %r...)" % want59[:40])
print("9. line 59: PENDING Tab's 6:40 wording — unchanged by design")

# ---------- verdict ---------------------------------------------------
for n in notes:
    print("NOTE:", n)
if failures:
    print("\nFAILURES (%d):" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL TAB R14 CHECKS PASSED (item 9 pending by instruction)")

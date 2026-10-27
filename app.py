"""
Solace — a private AI self-help companion for reflection and support (v2, full-stack).

Backend: FastAPI + SQLite. The AI brain is Anthropic's API, read from the
ANTHROPIC_API_KEY environment variable (set on the hosting provider).
For LOCAL development only, if the env var is missing, calls fall back to
the vault-backed dev CLI — never hardcode or commit any key.
"""
import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, BackgroundTasks, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "app.db")
CLI_PATH = os.path.expanduser("~/workspace/skills/anthropic/bin/claude-chat")

CHAT_MODEL = "claude-sonnet-4-6"
SUMMARY_MODEL = "claude-haiku-4-5"

CRISIS_PATTERNS = [
    # --- direct intent ---
    r"kill myself",
    r"kil+ myself",
    r"k[i1]ll\s*my\s*self",
    r"end my life",
    r"take my own life",
    r"end it all",
    r"suicid\w*",
    r"suisid\w*",
    r"self[\s-]?harm",
    r"hurt myself",
    r"harm myself",
    r"cut(?:ting)? myself",
    r"cut my wrists",
    r"off myself",
    r"do myself in",
    # --- desire to die / not exist ---
    r"wanna die",
    r"want to die",
    r"want 2 die",
    r"don'?t want to live",
    r"do not want to live",
    r"don'?t want to be here",
    r"don'?t want to exist",
    r"wish i (was|were) dead",
    r"wish i didn'?t exist",
    r"want it all to (end|stop)",
    # --- hopelessness / burden (indirect) ---
    r"no reason to live",
    r"no point (in|to) (living|going on|life)",
    r"what'?s the point of (living|life|going on)",
    r"better off dead",
    r"better off without me",
    r"(everyone|everybody|they|people) (would|will|'d) be better off",
    r"nobody would (care|miss me|notice)",
    r"if i (was|were) gone",
    r"tired of (living|life|being alive|existing)",
    r"can'?t (do this|go on|take it|keep going) anymore",
    r"cannot (go on|do this anymore)",
    r"sleep and (not|never) wake up",
    r"(wish|hope) i (would )?never wake up",
    r"not wake up (tomorrow|again)",
    r"don'?t want to wake up",
    r"(wish|want) (i could )?(to )?disappear",
    r"disappear forever",
    r"feel like (such )?a burden",
    r"(i am|i'?m) (such )?a burden",
    r"(started|been|keep) cutting",
    # --- plan / means ---
    r"pills saved",
    r"saved up (pills|meds|medication|tablets)",
    r"stockpil\w+ (pills|meds|medication)",
    r"have a plan (to|for) (end|kill|die)",
    r"how i'?d do it",
    r"wrote a (goodbye|suicide) (note|letter)",
    r"wrote goodbye letters",
    r"goodbye letters",
    r"goodbye (note|letter)",
    r"this is (my )?goodbye",
    r"overdos\w*",
    r"hang\w* myself",
    r"shoot myself",
    r"gun to my head",
    r"jump(ing)? off",
    r"slit my",
    # --- slang / coded ---
    r"kms",
    r"unaliv\w* myself",
    r"sewerslide",
    # Hebrew crisis phrasings (deterministic detection for Hebrew input)
    r"רוצה למות",
    r"להתאבד",
    r"אין טעם לחיים",
    r"לא רוצה לחיות",
    r"עדיף למות",
    r"רוצה לפגוע בעצמי",
    r"פוגע בעצמי",
]
CRISIS_RE = [re.compile(r"\b" + p + r"\b") for p in CRISIS_PATTERNS]

# Bare mentions of suicide in an informational context ("suicide prevention
# walk", "the hotline") must not fire the fixed crisis response on their
# own. Only the bare suicide-word patterns are guarded this way; every
# first-person intent/plan/means pattern above always fires.
BARE_SUICIDE_RE = [re.compile(r"\bsui[cs]id\w*\b")]
INFORMATIONAL_RE = re.compile(
    r"\b(prevention|awareness|hotline|helpline|volunteer\w*|fundrais\w*|"
    r"walk|charity|studying|research|article|movie|book|song|news)\b"
)

# Clearly informational QUESTIONS about the word itself ("What does the
# word suicide mean?", "define suicide") — third-person, definitional.
# These get a gentle answer + soft check-in, NOT the crisis script
# (Solace re-test fix #7). Only ever consulted when the bare suicide
# word is the sole possible signal; any first-person intent pattern
# and the classifier still override (fail-safe).
INFO_QUERY_RE = re.compile(
    r"(what does (the )?(word|term|phrase) [\w'-]+ mean"
    r"|what does ['\"]?sui[cs]id\w*['\"]? mean"
    r"|(meaning|definition) of (the word )?['\"]?sui[cs]id\w*"
    r"|define ['\"]?sui[cs]id\w*"
    r"|what is (the )?(meaning|definition) of"
    r"|how do you (say|spell) ['\"]?sui[cs]id\w*"
    r"|sui[cs]id\w* (means|meaning|definition)\b)"
)

# Fixed, human crisis response (spec: Solace Fix #1). Sent verbatim, with
# no model call, the moment risk is detected — and it REPLACES the normal
# reply.
FIXED_CRISIS_RESPONSE = (
    "I'm really glad you told me. I want to make sure you're safe right now. "
    "Are you thinking about acting on this tonight? You don't have to go "
    "through this alone — you can call or text 988 (Suicide & Crisis "
    "Lifeline, 24/7) or text HOME to 741741. If you're in immediate danger, "
    "call 911. I'm here with you — do you want to tell me what's going on?"
)

# The same fixed response, fully in Hebrew, for Hebrew conversations
# (re-test fix #8): ERAN 1201 first, then 988 / 741741. Immediate
# danger in Israel is MADA 101 — NOT 911 (Tab round fix: the Hebrew
# text used to end with "call 911", the wrong country).
FIXED_CRISIS_RESPONSE_HE = (
    "אני ממש שמח שסיפרת לי על זה. חשוב לי לוודא שאתה בטוח עכשיו. "
    "האם אתה חושב לפעול על פי המחשבות האלה הלילה? אתה לא צריך לעבור את זה לבד — "
    "בישראל אפשר להתקשר לער״ן, קו הסיוע הנפשי, במספר 1201 (זמין 24/7). "
    "בארה״ב אפשר להתקשר או לשלוח הודעה ל־988 (קו החיים למניעת התאבדות, 24/7), "
    "או לשלוח HOME ל־741741. אם אתה בסכנה מיידית, התקשר למד״א במספר 101. "
    "אני כאן איתך — אתה רוצה לספר לי מה עובר עליך?"
)

# Completed overdose / poisoning (Tab ruling, Oct 26): someone who
# has ALREADY swallowed pills is a medical emergency — the reply
# must LEAD with call 911 (in Hebrew, MADA 101), never with the
# suicide question. Deterministic and pre-model, like the crisis
# path, but a SEPARATE response: the frozen crisis strings stay
# untouched. Only completed-ingestion phrasing fires this; plans
# ("going to overdose tonight", "pills saved up") keep the frozen
# crisis path, and ordinary medication talk ("I took my pills this
# morning") never fires.
OVERDOSE_RE = [
    re.compile(p, re.I) for p in (
        r"(?:took|swallowed|ate)\b[^.!?\n]{0,25}?"
        r"(?:all\s+(?:the|my)|whole|entire|too\s+many|handful|bottle|"
        r"\d+\s*(?:pills|tablets))",
        r"took\s+an?\s+overdose",
        r"\boverdosed\b",
        r"swallowed\s+a\s+bottle",
    )
] + [
    re.compile(p) for p in (
        # Completed ingestion, Hebrew: a past-tense swallowing verb
        # + a quantity word + pills. "הרבה כדורים" (Tab's exact live
        # case, Oct 26) was missed by the first version, which only
        # knew כל הכדורים / יותר מדי / בקבוק. Ordinary medication
        # talk stays safe: it needs the quantity word — "לקחתי את
        # הכדורים שלי הבוקר" has none, and present-tense "אני לוקח
        # הרבה כדורים כל יום" uses לוקח, not a past-tense verb.
        r"(?:בלעתי|לקחתי|אכלתי)[^.!?\n]{0,20}?"
        r"(?:כל הכדורים|כל הגלולות|הרבה כדורים|הרבה גלולות|"
        r"המון כדורים|המון גלולות|מלא כדורים|מלא גלולות|"
        r"יותר מדי|בקבוק|חופן)",
        r"מנת\s+יתר",
        r"הרעלת\s+תרופות",
    )
]


# Tab round 3 (his "r9" test): "בלעתי כדורים" — swallowed pills,
# with NO quantity word — reached the suicide question in 1 of 2
# runs instead of מד״א 101 right away: the quantity patterns above
# missed it and only the (probabilistic) classifier stood behind
# them. A first-person past-tense ingestion verb + plural pills
# IS a completed ingestion; no quantity word is required. The
# ordinary-medication guards below keep prescribed talk safe:
# a single pill, "my pills", "my medication", "as the doctor
# said" never fire this pattern.
_HE_INGEST_PILLS_RE = re.compile(
    r"(?:בלעתי|בלע|בלעה|לקחתי|אכלתי)[^.!?\n]{0,20}?"
    r"(?:כדורים|גלולות)")
_HE_MED_GUARD_RE = re.compile(
    r"(?:כדור\s+אחד|הכדור\s+שלי|הכדורים\s+שלי|הגלולות\s+שלי|"
    r"התרופה\s+שלי|כמו\s+שהרופא\s+אמר|לפי\s+הוראות\s+הרופא|"
    r"במינון\s+הרגיל)")


def detect_overdose(text):
    """True only for COMPLETED ingestion (already swallowed) — the
    medical-emergency subset of crisis signals."""
    t = text or ""
    if any(rx.search(t) for rx in OVERDOSE_RE):
        return True
    if _HE_INGEST_PILLS_RE.search(t) \
            and not _HE_MED_GUARD_RE.search(t):
        return True
    return False


EMERGENCY_RESPONSE = (
    "Call 911 right now — this is a medical emergency, and getting "
    "help fast matters more than anything else. If you can, tell "
    "them what you took and how much, and stay on the line with "
    "them. If someone is near you, ask them to call or to stay "
    "with you. I'm here with you — please make that call now."
)
EMERGENCY_RESPONSE_HE = (
    "התקשר עכשיו למד״א במספר 101 — זהו מצב חירום רפואי, וקבלת עזרה "
    "מהר חשובה יותר מכל דבר אחר. אם אתה יכול, ספר להם מה לקחת וכמה, "
    "והישאר איתם על הקו. אם יש מישהו לידך, בקש שיתקשר או שיישאר "
    "איתך. אני כאן איתך — בבקשה תתקשר עכשיו."
)

# Spouse loss + crisis (Tab ruling, Oct 26): a widow(er) whose
# message carries risk ("I'm a widow, 47 years… I don't see the
# point") gets a crisis reply that NAMES the loss — one line about
# her husband (or his wife) — while keeping the safety question,
# the 988 resources and the banner. The generic closer of the
# frozen reply ("tell me what's going on") is wrong here, and the
# frozen bytes may not change — so, like the retraction check-in,
# this is a separate deterministic response used only on this
# route; every other crisis keeps the frozen reply verbatim.
_SPOUSE_LOSS_EN = [
    (re.compile(r"\bwidow\b", re.I), "husband"),
    (re.compile(r"\bwidower\b", re.I), "wife"),
    (re.compile(r"\bmy\s+(husband|wife)\s+"
                r"(?:died|passed\s+away|was\s+killed|is\s+gone)\b",
                re.I), None),
    (re.compile(r"\blost\s+my\s+(husband|wife)\b", re.I), None),
    (re.compile(r"\bmy\s+late\s+(husband|wife)\b", re.I), None),
]
_SPOUSE_LOSS_HE = [
    (re.compile(r"אלמנה"), "husband"),
    (re.compile(r"אלמן"), "wife"),
    (re.compile(r"בעלי\s+(?:נפטר|מת|נהרג)"), "husband"),
    (re.compile(r"אשתי\s+(?:נפטרה|מתה|נהרגה)"), "wife"),
    (re.compile(r"איבדתי\s+את\s+בעלי"), "husband"),
    (re.compile(r"איבדתי\s+את\s+אשתי"), "wife"),
]


def detect_spouse_loss(text):
    """'husband' / 'wife' when this message discloses a spouse's
    death (widow/widower, 'my husband died', 'lost my wife',
    אלמנה / בעלי נפטר …), else None."""
    t = text or ""
    for rx, fixed in _SPOUSE_LOSS_HE:
        if rx.search(t):
            return fixed
    for rx, fixed in _SPOUSE_LOSS_EN:
        m = rx.search(t)
        if m:
            return fixed or m.group(1).lower()
    return None


def spouse_crisis_response(spouse, hebrew):
    """The widow-route crisis reply: loss line first, then the
    safety question and resources (988 / ער״ן 1201), banner and
    latch handled by the caller exactly as for the frozen reply."""
    if hebrew:
        who = "בעלך" if spouse == "husband" else "אשתך"
        pronoun = "אותו" if spouse == "husband" else "אותה"
        return (
            "אני ממש מצטער על %s. לאבד %s משנה הכול — ועכשיו חשוב "
            "לי לוודא שאתה בטוח. האם אתה חושב לפעול על פי המחשבות "
            "האלה הלילה? אתה לא צריך לעבור את זה לבד — בישראל "
            "אפשר להתקשר לער״ן, קו הסיוע הנפשי, במספר 1201 "
            "(זמין 24/7). אם אתה בסכנה מיידית, התקשר למד״א במספר "
            "101. אני כאן איתך." % (who, pronoun)
        )
    who = "husband" if spouse == "husband" else "wife"
    pronoun = "him" if spouse == "husband" else "her"
    return (
        "I'm so sorry about your %s. Losing %s changes everything "
        "— and right now I want to make sure you're safe. Are you "
        "thinking about acting on these thoughts tonight? You "
        "don't have to go through this alone — you can call or "
        "text 988 (Suicide & Crisis Lifeline, 24/7) or text HOME "
        "to 741741. If you're in immediate danger, call 911. "
        "I'm here with you." % (who, pronoun)
    )

# Identity questions (Tab ruling, Oct 26): "Is this therapy?" and
# "Are you a psychologist?" must get a PLAIN "No" — deterministically,
# not a model essay that then has to survive the word filter. The
# patterns are anchored to a message that IS the identity question
# (a risk-bearing message that merely mentions these words keeps
# the classifier/crisis paths, which run before this one), and the
# route is disabled while crisis mode is latched.
_IDENTITY_Q_RES = [
    ("therapy", re.compile(
        r"^\s*(?:is|isn'?t)\s+(?:this|that|solace)\s+"
        r"(?:just\s+|really\s+|real\s+)?"
        r"(?:therapy|counseling|counselling)\b", re.I)),
    ("therapy", re.compile(
        r"^\s*(?:האם\s+)?(?:זה|זו)\s+(?:באמת\s+)?טיפול\b")),
    ("professional", re.compile(
        r"^\s*are\s+you\s+(?:a\s+|an\s+|my\s+)?"
        r"(?:real\s+|licensed\s+)?"
        r"(?:therapist|psychologist|counselor|counsellor|"
        r"psychiatrist|shrink)\b", re.I)),
    ("professional", re.compile(
        r"^\s*(?:האם\s+)?(?:אתה|את|אתם)\s+"
        r"(?:פסיכולוג|פסיכולוגית|פסיכולוגים|מטפל|מטפלת)\b")),
    # Request form (Tab round 3): "Can you be my therapist?" is
    # the same identity question wearing a request — it gets the
    # same plain deterministic "No", never a model reply whose
    # scrubbed remains open with a dangling "What I can do…".
    ("therapy", re.compile(
        r"^\s*(?:can|could|will|would)\s+you\s+(?:please\s+)?"
        r"(?:be|become|act\s+as)\s+(?:my\s+|a\s+|the\s+)?"
        r"(?:therapist|psychologist|counselor|counsellor|"
        r"psychiatrist|shrink)\b", re.I)),
    ("therapy", re.compile(
        r"^\s*(?:please\s+)?be\s+my\s+"
        r"(?:therapist|psychologist|counselor|counsellor)\b", re.I)),
    ("therapy", re.compile(
        r"(?:אתה|את)\s+יכול(?:ה)?\s+להיות\s+(?:ה)?"
        r"(?:מטפל|פסיכולוג)(?:\s+שלי)?")),
    ("therapy", re.compile(
        r"יכול(?:ה)?\s+להיות\s+(?:ה)?(?:מטפל|פסיכולוג)\s+שלי")),
    ("therapy", re.compile(
        r"תהיה\s+(?:ה)?(?:מטפל|פסיכולוג)\s+שלי")),
]

IDENTITY_ANSWERS = {
    ("therapy", False): (
        "No. Not therapy — I'm Solace, an AI companion for "
        "reflection and self-help, here to talk things through "
        "with you."
    ),
    ("therapy", True): (
        "לא — זה לא טיפול. אני סולאס, מלווה בינה מלאכותית "
        "להרהור ולתמיכה עצמית, כאן כדי לדבר איתך על מה שעובר עליך."
    ),
    ("professional", False): (
        "No. I'm Solace — an AI companion for reflection and "
        "self-help, not a licensed professional."
    ),
    ("professional", True): (
        "לא. אני סולאס — מלווה בינה מלאכותית להרהור ולתמיכה "
        "עצמית, לא איש מקצוע מורשה."
    ),
}


def detect_identity_question(text):
    """'therapy' / 'professional' / None — the message IS a direct
    question about what Solace is (see _IDENTITY_Q_RES)."""
    t = text or ""
    for kind, rx in _IDENTITY_Q_RES:
        if rx.search(t):
            return kind
    return None


# Availability questions (Tab round 3): "Are you available 24/7?"
# got a model "Yes… any time of day or night, any day of the
# week". Solace makes NO availability promise, so — like the
# identity questions — this is answered deterministically with a
# non-claim that simply returns to the conversation. (A message
# that merely mentions these words mid-sentence is not the
# question; the risk paths run before this one regardless.)
_AVAILABILITY_Q_RES = [
    re.compile(r"\b24\s*/\s*7\b"),
    re.compile(
        r"^\s*are\s+you\s+(?:available|there|around|online)\b", re.I),
    re.compile(
        r"\bare\s+you\s+always\s+(?:available|there|around)\b", re.I),
    re.compile(
        r"\bcan\s+i\s+(?:talk|message|text|write|reach)(?:\s+(?:to\s+)?you)?"
        r"\s+(?:any\s?time|whenever|at\s+any\s+time)\b", re.I),
    re.compile(r"(?:אתה|את)\s+זמי\w*\s*(?:תמיד|בכל\s+שעה|24\s*/\s*7)"),
    re.compile(r"זמי\w*\s+(?:24\s*/\s*7|מסביב\s+לשעון)"),
    re.compile(r"אפשר\s+לדבר\s+איתך\s+בכל\s+שעה"),
    re.compile(r"^\s*(?:האם\s+)?(?:אתה|את)\s+זמי\w*\s*\?\s*$"),
]

AVAILABILITY_ANSWER = (
    "I'm here whenever you open Solace — what's on your mind?"
)
AVAILABILITY_ANSWER_HE = (
    "אני כאן כשתפתח את סולאס — מה עובר עליך?"
)


# Need-a-professional questions (Tab round 3): "אני צריך
# פסיכולוג?" ("Do I need a psychologist?") never suggested a
# professional at all. This is a different intent from "are YOU
# a professional" (plain "No", above): the person is asking
# about THEMSELVES, and the reply must carry the approved
# referral line — appended deterministically when the model's
# reply omitted it (see _finish_model_turn).
REFERRAL_PHRASE_EN = "a doctor or a licensed mental health professional"
REFERRAL_PHRASE_HE = "רופא או איש מקצוע מורשה בתחום בריאות הנפש"
REFERRAL_APPEND_EN = (
    "If outside support ever feels right, you can talk to "
    "a doctor or a licensed mental health professional about it."
)
REFERRAL_APPEND_HE = (
    "אם תרצה תמיכה מבחוץ, אפשר לדבר על זה עם "
    "רופא או איש מקצוע מורשה בתחום בריאות הנפש."
)
_NEED_PROF_RES = [
    re.compile(
        r"\b(?:do|should|would|could)\s+i\s+(?:really\s+)?"
        r"(?:need|see|get|find|talk\s+to|go\s+to)\s+"
        r"(?:a\s+|an\s+|to\s+)?"
        r"(?:therapist|psychologist|counselor|counsellor|"
        r"psychiatrist|shrink|professional)\b", re.I),
    re.compile(
        r"\bi\s+(?:think\s+i\s+|really\s+|just\s+)?need\s+"
        r"(?:a\s+|to\s+see\s+a\s+)?"
        r"(?:therapist|psychologist|counselor|counsellor|"
        r"professional)\b", re.I),
    re.compile(r"\bdo\s+i\s+need\s+professional\s+help\b", re.I),
    re.compile(
        r"(?:צריך|צריכה|כדאי|מחפש|מחפשת)[^.!?\n]{0,15}?"
        r"(?:פסיכולוג|פסיכולוגית|מטפל|מטפלת|איש\s+מקצוע|"
        r"עזרה\s+מקצועית)"),
    re.compile(
        r"(?:פסיכולוג|מטפל)[^.!?\n]{0,12}?(?:צריך|כדאי)"),
]


def detect_need_professional(text):
    """True when the person asks whether THEY need / should see
    a professional (not whether Solace is one)."""
    t = text or ""
    return any(rx.search(t) for rx in _NEED_PROF_RES)


def _ensure_referral(reply, hebrew):
    """Guarantee the approved referral line is present (in the
    message's language) without disturbing anything else."""
    if hebrew:
        if REFERRAL_PHRASE_HE in reply:
            return reply
        return reply + "\n\n" + REFERRAL_APPEND_HE
    if REFERRAL_PHRASE_EN.lower() in reply.lower():
        return reply
    return reply + "\n\n" + REFERRAL_APPEND_EN


def detect_availability_question(text):
    """True when the message asks whether Solace is available
    (24/7, always, any time) — answered without any claim. A bare
    "24/7" mention fires only in a question-shaped message, so
    "my support group meets 24/7" stays an ordinary message."""
    t = text or ""
    if any(rx.search(t) for rx in _AVAILABILITY_Q_RES[1:]):
        return True
    if re.search(r"\b24\s*/\s*7\b", t):
        return "?" in t or bool(
            re.match(r"\s*(are|can|do|is|will)\b", t, re.I))
    return False


# Gentle check-in sent ONCE when a person walks back a crisis statement
# while crisis mode is latched (re-test fix #2). A retraction no longer
# switches safety mode off instantly: this check-in goes out, crisis
# mode stays latched, and only the NEXT calm, non-risk exchange steps
# back down to normal conversation. No relief emoji, no instant "that's a relief".
RETRACTION_CHECKIN_RESPONSE = (
    "I'm really glad to hear that — and I hope you understand, I'd still "
    "rather check than just move on: are you safe right now? You shared "
    "something serious a moment ago, and your safety matters more than "
    "anything else here. If anything changes, you can call or text 988 "
    "any time — free, confidential, 24/7 — or text HOME to 741741. "
    "And I'm right here with you; we can keep talking about whatever "
    "you need."
)
RETRACTION_CHECKIN_RESPONSE_HE = (
    "אני ממש שמח לשמוע את זה — ובכל זאת חשוב לי לבדוק ולא פשוט לעבור הלאה: "
    "אתה בטוח עכשיו? סיפרת לי משהו רציני לפני רגע, והבטיחות שלך חשובה יותר מהכול. "
    "אם משהו משתנה, בישראל אפשר להתקשר לער״ן במספר 1201 בכל שעה, "
    "ובארה״ב ל־988. אני כאן איתך — אפשר להמשיך לדבר על מה שאתה צריך."
)

# Dedicated prompt for clearly informational questions about the word
# itself (re-test fix #7): answer the question, gentle check-in in the
# spirit of the essay path, soft resource line. Never the crisis script.
INFO_SYSTEM_PROMPT = """You are Solace, a warm AI self-help companion for reflection and support (not a licensed professional). The person has asked a clearly informational question about the word or topic of suicide — for example what the word means.

Do exactly this, in a few short sentences:
1. Answer their actual question directly, accurately and plainly (e.g. give the definition they asked for).
2. Add ONE gentle check-in sentence in this spirit: sometimes people look this up because it connects to something they're going through personally, and sometimes it's purely academic or curiosity — either is completely okay.
3. End with one soft, low-key resource line: if it ever does feel personal, in the US they can call or text 988 any time.

Never ask "are you thinking about acting on this tonight?", never use a crisis script, never alarm. If the person is writing in Hebrew, answer fully in Hebrew and name Israel's ERAN hotline (1201) as the resource instead."""

# Deterministic fallback for the informational path if the model call
# fails — same gentle shape as the essay path (fix #7).
INFO_FALLBACK = (
    "\u201cSuicide\u201d means the act of a person intentionally ending "
    "their own life. I wanted to answer that plainly \u2014 and to check "
    "in gently: sometimes people look this up because it connects to "
    "something personal, and sometimes it's purely for school or "
    "curiosity. Either is completely okay, and I'm here if you ever "
    "want to talk. If it ever does feel personal, in the US you can "
    "call or text 988 any time."
)
INFO_FALLBACK_HE = (
    "\u05f3התאבדות\u05f3 היא מעשה שבו אדם מסיים את חייו בכוונה. "
    "רציתי לענות על זה בפשטות \u2014 וגם לבדוק בעדינות: לפעמים אנשים "
    "מחפשים את זה כי זה מתחבר למשהו אישי, ולפעמים זה פשוט מסקרנות או "
    "ללימודים. שני הדברים בסדר גמור, ואני כאן אם תרצה לדבר. אם זה "
    "אי פעם מרגיש אישי, בישראל אפשר להתקשר לער\u05f4ן במספר 1201 בכל שעה."
)

# Grief path (Tab persona test): a bereavement disclosure gets a
# FIXED warm opening line in front of the model's reply —
# deterministic, like the other fixed paths, but deliberately NOT
# the crisis flow: no latch, no banner, no hotline footer, and the
# conversation continues normally underneath it. (If the same
# message also carries risk, the crisis path runs first and grief
# never fires.)
GRIEF_OPENING = (
    "I'm so sorry for your loss. Losing someone you love changes "
    "everything, and there is no right way to grieve. I'm here with "
    "you — take all the time you need. Whenever you're ready, I'd "
    "love to hear about them."
)
GRIEF_OPENING_HE = (
    "אני ממש מצטער על האובדן שלך. לאבד מישהו שאוהבים משנה הכול, "
    "ואין דרך נכונה להתאבל. אני כאן איתך — קח את כל הזמן שאתה צריך. "
    "כשתרצה, אשמח לשמוע עליו."
)
# Feminine-deceased variant (Tab ruling, Oct 26): when the person
# who died is a mother / sister / grandmother / wife / daughter,
# the fixed line must say עליה — the masculine עליו was wrong for
# every mother. Only the final pronoun differs.
GRIEF_OPENING_HE_F = (
    "אני ממש מצטער על האובדן שלך. לאבד מישהו שאוהבים משנה הכול, "
    "ואין דרך נכונה להתאבל. אני כאן איתך — קח את כל הזמן שאתה צריך. "
    "כשתרצה, אשמח לשמוע עליה."
)
_GRIEF_KIN_HE_F = re.compile(
    r"(אמא|סבתא|אחותי|אחות\s+שלי|אשתי|הבת\s+שלי|בת\s+שלי|חברה\s+שלי)")
_GRIEF_KIN_HE_M = re.compile(
    r"(אבא|סבא|אחי|אח\s+שלי|בעלי|הבן\s+שלי|בן\s+שלי|חבר\s+שלי)")


def detect_grief_kin_he(text):
    """'f' / 'm' / None — the gender of the deceased kin named in a
    Hebrew grief message (feminine checked first: אחותי must never
    be read as a brother)."""
    t = text or ""
    if _GRIEF_KIN_HE_F.search(t):
        return "f"
    if _GRIEF_KIN_HE_M.search(t):
        return "m"
    return None


def grief_opening_for(hebrew, msg):
    """The exact fixed opening for this grief turn: Hebrew gets the
    gender-correct pronoun for the person who died."""
    if not hebrew:
        return GRIEF_OPENING
    if detect_grief_kin_he(msg) == "f":
        return GRIEF_OPENING_HE_F
    return GRIEF_OPENING_HE
# Continuation used only if the model call fails on a grief turn.
GRIEF_FALLBACK = (
    "I'm here — we don't have to figure anything out right now. "
    "We can just sit with this for a while."
)
GRIEF_FALLBACK_HE = (
    "אני כאן — לא צריך להבין הכול עכשיו. אפשר פשוט להיות עם זה קצת."
)
GRIEF_NOTE = (
    "\n\nGRIEF: This person has just told you that someone they "
    "love has died. Their reply will BEGIN with a fixed condolence "
    "line that is added for you — do not repeat it or rephrase it; "
    "continue from it. Acknowledge the loss and the person who "
    "died; invite them to share memories or feelings at their own "
    "pace; stay with the grief. Do NOT offer a practical exercise, "
    "techniques, or next steps in this reply, do not rush to "
    "comfort or fix, and keep it short and human. If they express "
    "any wish to harm themselves, follow the crisis protocol."
)

# Hebrew grief grammar directive (Tab round 2): the model's own
# Hebrew continuation flipped masculine/feminine mid-reply — it
# wrote about a mother with masculine forms (עליו, ממנו) under the
# feminine fixed opening. The note pins the deceased's gender,
# the addressee forms used by the fixed opening, and bans the
# slash-hedge (מקשיב/ה) and separator lines.
_GRIEF_GENDER_NOTES = {
    "f": (
        "\n\nHEBREW GRAMMAR — CRITICAL for this reply: the person "
        "who died is a WOMAN. Refer to her ONLY in feminine forms "
        "(היא, עליה, הייתה, שלה) — never עליו, הוא, היה or ממנו "
        "about her. Address the person you are speaking with in "
        "masculine singular, exactly as the opening line does "
        "(קח את הזמן, כשתרצה). Never use slash forms such as "
        "מקשיב/ה — choose the one matching form and stay with it. "
        "Never write a separator line such as ---: write ONE "
        "continuous reply that continues from the opening line."
    ),
    "m": (
        "\n\nHEBREW GRAMMAR — CRITICAL for this reply: the person "
        "who died is a MAN. Refer to him ONLY in masculine forms "
        "(הוא, עליו, היה, שלו) — never עליה, היא, הייתה or ממנה "
        "about him. Address the person you are speaking with in "
        "masculine singular, exactly as the opening line does "
        "(קח את הזמן, כשתרצה). Never use slash forms such as "
        "מקשיב/ה — choose the one matching form and stay with it. "
        "Never write a separator line such as ---: write ONE "
        "continuous reply that continues from the opening line."
    ),
    None: (
        "\n\nHEBREW GRAMMAR — CRITICAL for this reply: keep ONE "
        "consistent gender for the person who died (the forms of "
        "the opening line govern), address the person you are "
        "speaking with exactly as the opening line does, and "
        "never use slash forms such as מקשיב/ה. Never write a "
        "separator line such as ---: write ONE continuous reply "
        "that continues from the opening line."
    ),
}

# Tab round 3: whatever the kin note above says, the person is
# addressed in ONE gender throughout — if a second-person form
# is ever uncertain, a gender-neutral construction (infinitive
# or noun form) is used instead of switching gender mid-reply.
for _gk in _GRIEF_GENDER_NOTES:
    _GRIEF_GENDER_NOTES[_gk] += (
        " Address the person in ONE gender from start to finish; "
        "if a second-person form is uncertain, use a "
        "gender-neutral construction (an infinitive or a noun "
        "form) — never switch gender mid-reply."
    )

# System prompt used for follow-up turns while crisis mode is latched:
# safety only — no techniques, no homework, no jargon.
CRISIS_SYSTEM_PROMPT = """You are Solace, in CRISIS MODE with a person who has expressed thoughts of suicide or self-harm. Rules for every reply in this mode:
- Your only job is the person's safety right now. Stay warm, steady, human. Take everything seriously; never panic, never moralize or shame.
- Ask directly how they are doing and whether they are safe right now / tonight. Keep gently checking safety each reply.
- In every reply, remind them: in the US they can call or text 988 (Suicide & Crisis Lifeline, 24/7) or text HOME to 741741; if they are in immediate danger, call 911.
- Match the person's language: reply in the language they are writing in. If they write in Hebrew, your ENTIRE reply must be in Hebrew — not English with a Hebrew line added — and the resources lead with Israel's ERAN emotional-support hotline (1201) first, then 988 / 741741; for immediate danger in Israel the number is MADA (מד״א) 101 — never give 911 to a person in Israel.
- Do NOT use structured techniques or exercises: no CBT/ACT/DBT methods, no thought-challenging, no homework, no analysis of their patterns, no jargon (never say things like "resistance" or "emotional conditioning").
- Short replies (2-4 sentences). One caring question at a time. Stay with them; do not rush to wrap up."""

CLASSIFIER_SYSTEM = (
    "You are a safety classifier for a support app. Decide "
    "whether the user's message expresses suicidal thoughts, intent or a "
    "plan/means to harm themselves, or a wish to be dead — including "
    "indirect, coded, or slang phrasing. Benign, informational, or clearly "
    "retracted statements are SAFE. Reply with exactly one word: RISK or SAFE."
)

# Negation words that, appearing just before a crisis phrase, turn it into a
# retraction (e.g. "I don't wanna die", "אני לא רוצה למות").
NEGATION_RE = re.compile(
    r"\b(don'?t|do not|doesn'?t|does not|didn'?t|did not|won'?t|wouldn'?t|"
    r"never|no longer|not|isn'?t|aren'?t|can'?t|couldn'?t|shouldn'?t|"
    r"לא|אף פעם)\b"
)

# Explicit walk-backs. These alone de-escalate; they never trigger crisis.
# Includes negated crisis statements ("I don't wanna die") so a clear
# disavowal de-escalates even as the first message of a session.
RETRACTION_RE = re.compile(
    r"\b(didn'?t mean it|did not mean it|just kidding|just joking|"
    r"i was joking|i'?m fine now|i am fine now|i'?m ok(ay)?|i am okay|"
    r"i feel better|false alarm|i take it back|"
    r"don'?t wanna die|do not wanna die|don'?t want to die|"
    r"do not want to die|don'?t want to kill myself|"
    r"do not want to kill myself|לא רוצה למות|"
    r"אני בסדר|הכול בסדר|הכל בסדר|רק צחקתי|סתם צחקתי|תשכח מזה|לא התכוונתי)\b"
)

SYSTEM_PROMPT = """You are Solace, a warm and skilled AI self-help companion for reflection and support. You draw deeply on evidence-based approaches — CBT, ACT, DBT, trauma-informed care, attachment theory, grief work, motivational interviewing — but you are NOT a licensed professional, and this is NOT a substitute for professional care.

HARD LIMITS (never break these):
- Never tell someone what condition they have. Never say or imply "you have depression / anxiety / PTSD / etc." Use describing language only, never labels: "a working understanding," "it sounds like," "what I'm hearing is."
- Never claim to be a licensed professional or a human being.
- IDENTITY LANGUAGE (Tab persona test): never describe yourself, or what happens in this conversation, as therapy, treatment, or counseling, and never as the work of a therapist, psychologist, or counselor — in English, Hebrew, or any language. You are an AI self-help companion for reflection and support. Never say you diagnose, heal, or cure.
- REFERRAL WORDING (Tab ruling): encouraging outside support is welcome, but ALWAYS write it with exactly this phrasing — "a doctor or a licensed mental health professional" (in Hebrew: "רופא או איש מקצוע מורשה בתחום בריאות הנפש"). Never use any other label for them; other wordings are removed from your replies before they are shown.
- PRIVACY REASSURANCE (Tab ruling): if the person worries about who might find out what they share, reassure them with exactly this and nothing stronger: "Your chats aren't shared with other people or used to advertise to you. See the privacy page for details." Never promise that no one will ever see or read what they share.
- This is not emergency care. If the person is in crisis, follow the crisis protocol below.
- Gently encourage real help beyond Solace for serious or persistent struggles — a doctor or a licensed mental health professional (Hebrew: רופא או איש מקצוע מורשה בתחום בריאות הנפש).

FIRST-CONVERSATION INTAKE (when this is a new person — no summary of earlier conversations exists):
You have been given their name, current mood, and what's weighing on them (see "About this person"). Do NOT re-ask what you already know.
1. Open warmly. Reflect back what they shared so they feel truly heard.
2. Continue the intake conversationally, ONE question at a time. Across this first conversation, naturally cover: how this has been affecting their daily life and sleep, what support they have around them, how they've been coping so far, and what they'd most want to be different.
3. Conversational, never a checklist or interrogation. Short replies (2-4 sentences), then one question.
4. Toward the end of the intake, share a brief "working understanding" of the person and their situation, gently, and suggest ONE small, concrete first step they could try this week.

ONGOING SESSIONS (when a summary of earlier sessions exists):
- Greet them warmly, show you remember what matters, ask how they've been since last time.
- ANSWER FIRST: if the person asks you a question, answer it first and directly. Only after answering, if it fits naturally, weave in what you remember. Never ignore their question to ask about a remembered item.
- Reflective listening first: mirror their words and feelings before offering anything new.
- Draw on evidence-based approaches as they fit the moment: CBT (noticing and gently examining unhelpful thought patterns), ACT (values, making room for difficult feelings), DBT (distress tolerance, emotion regulation skills), motivational interviewing (open questions, affirming their strengths), trauma-informed care (safety and choice first — never push anyone to disclose), attachment and grief awareness.
- Adapt to their concern. Don't force techniques. Meet them where they are.
- End each session with one small, doable next step.

PRACTICAL WORK:
- Talking alone isn't enough — give the person something concrete to work on between conversations. In each conversation, offer ONE practical exercise, practice, or small experiment tailored to their situation.
- Examples of the kind of thing to assign (pick what fits, one at a time): a thought-challenging exercise for spiraling thoughts ("when the thought comes, write it down and ask: what would I tell a friend thinking this?"); a tiny behavioral-activation step for low mood (one 10-minute walk, one message to a friend); a grounding practice for overwhelm (slow breathing, the 5-4-3-2-1 senses exercise); a short journaling prompt; a sleep wind-down routine; a small communication script to try with someone.
- Explain briefly WHY it helps, in plain words — one sentence, no lecture.
- Make it small enough they'll actually do it. Agree on it together, don't prescribe: "Would you be open to trying...?"
- Next session, follow up: ask how it went, what they noticed. Build on it or adjust. This is how progress compounds.
- Don't wait until the very end of a long session: once you understand the core struggle (usually within a few exchanges), offer the exercise in the next turn or two — people come for help they can use, not just conversation.

STYLE:
- Warm, calm, human, non-judgmental. Plain everyday language, no jargon.
- Short messages. One question at a time — never stack multiple questions.
- Never lecture, never give long lists of advice.

READING BETWEEN THE LINES (the inner person):
- People rarely say exactly what they feel. Listen for what's underneath: the fear behind the anger, the loneliness behind "I'm fine," the shame behind the joke, what they avoid saying, what they repeat, where their words and feelings don't match.
- Gently name what you sense, tentatively, never as fact: "I wonder if underneath the frustration there might be some hurt too — does that land at all?" Let them correct you; being wrong is fine, feeling seen is the point.
- Notice patterns across the conversation: recurring themes, contradictions, things they circle back to. Reflect the deeper thread, not just the last sentence.
- The goal is that the person feels understood at the level of who they truly are inside — not just their surface words.

CRISIS PROTOCOL:
If the person expresses thoughts of suicide, self-harm, or harming others:
1. Begin your reply with exactly [CRISIS] on its own first line (it is removed before they see it).
2. Respond with steady warmth: take it seriously, don't panic, don't moralize or shame.
3. Urge them to reach out right now: in the US call or text 988; anywhere else, contact local emergency services or a trusted person immediately. If (and only if) the person is writing in Hebrew, mention Israel's ERAN emotional-support hotline (1201) prominently; for non-Hebrew messages do not mention ERAN.
4. Keep it brief and human. Stay with them emotionally.

DE-ESCALATION:
If the person clearly walks back what they said ("I don't wanna die", "I didn't mean it", "I'm fine now", "I was just venting") or the conversation has plainly moved on, do NOT repeat the crisis protocol and do NOT start your reply with [CRISIS]. One brief, warm check-in is enough ("I'm really glad to hear that — how are you feeling right now?"), then continue the conversation normally. Never treat an ordinary follow-up message as a crisis just because an earlier message was one."""


# ---------------------------------------------------------------- DB ----
db_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_lock:
        conn = _conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT DEFAULT '',
                age_range TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS summaries (
                user_id TEXT PRIMARY KEY,
                summary TEXT DEFAULT '',
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS memory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'theme',
                text TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS memory_tombstones (
                user_id TEXT NOT NULL,
                norm TEXT NOT NULL,
                created_at TEXT DEFAULT '',
                PRIMARY KEY (user_id, norm)
            );
            CREATE TABLE IF NOT EXISTS rehearsals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                person_type TEXT DEFAULT '',
                person_name TEXT DEFAULT '',
                relationship TEXT DEFAULT '',
                topic TEXT DEFAULT '',
                reactions TEXT DEFAULT '[]',
                context TEXT DEFAULT '',
                state TEXT DEFAULT 'rehearsing',
                transcript TEXT DEFAULT '[]',
                debrief TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS real_talk_followups (
                user_id TEXT PRIMARY KEY,
                person_display TEXT NOT NULL DEFAULT '',
                lang TEXT NOT NULL DEFAULT 'en',
                due_at TEXT NOT NULL,
                asked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            """
        )
        # Migration: persistent crisis-mode latch per user (Solace Fix #1).
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
        if "crisis_mode" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN crisis_mode INTEGER DEFAULT 0"
            )
        # Re-test fix #2: step-down state + session-level banner flag.
        # crisis_stepdown: the gentle post-retraction check-in has been
        # given and one calm exchange is still owed before unlatching.
        # crisis_banner: once any crisis happened in this session, the
        # 988 banner stays visible for the rest of the session, even
        # after crisis mode steps back down.
        if "crisis_stepdown" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN crisis_stepdown INTEGER DEFAULT 0"
            )
        if "crisis_banner" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN crisis_banner INTEGER DEFAULT 0"
            )
            conn.execute(
                "UPDATE users SET crisis_banner=1 WHERE crisis_mode=1"
            )
        # Polish round: language of the session's crisis ('en'/'he'),
        # recorded when crisis mode latches, so the client renders
        # the sticky banner in the conversation's language (Hebrew
        # banner with ERAN 1201 for Hebrew crises).
        if "crisis_lang" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN crisis_lang TEXT DEFAULT 'en'"
            )
        # Tab fix: tombstones carry their deletion time, so a theme
        # the person raises again AFTER deleting it can come back,
        # while a memory rewrite can never resurrect it on its own.
        tcols = [r["name"] for r in conn.execute(
            "PRAGMA table_info(memory_tombstones)")]
        if "created_at" not in tcols:
            conn.execute(
                "ALTER TABLE memory_tombstones ADD COLUMN created_at TEXT DEFAULT ''"
            )
        conn.commit()
        conn.close()


# ------------------------------------------------------- Anthropic ----
def call_anthropic(model, system, messages, max_tokens=1024):
    """Call the Anthropic Messages API.

    Production path: ANTHROPIC_API_KEY env var (set on the host).
    Local-dev fallback: vault-backed CLI. Never hardcode a key.
    """
    payload = {
        "model": model,
        "system": system,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        with httpx.Client(timeout=120) as client:
            r = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        return "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        ).strip()
    if not os.path.exists(CLI_PATH):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set and the local dev CLI is unavailable."
        )
    p = subprocess.run(
        [CLI_PATH], input=json.dumps(payload), capture_output=True, text=True, timeout=180
    )
    if p.returncode != 0:
        raise RuntimeError("AI request failed: %s" % p.stderr.strip()[:300])
    return p.stdout.strip()


def detect_crisis_keywords(text):
    """Negation-aware crisis detection on the current message only.

    A crisis phrase counts only when it is NOT negated just before it
    ("I don't wanna die" is a retraction, not a crisis). Explicit
    retraction phrases de-escalate. An un-negated crisis phrase always
    wins over a retraction phrase in the same message (safety first).

    Bare mentions of the suicide word in an informational context
    ("suicide prevention walk") do not fire on their own.
    """
    t = text.lower()
    bare_only = True
    for rx in CRISIS_RE:
        for m in rx.finditer(t):
            # Look at the ~2 words immediately before the match for a
            # negation ("don't", "no longer", "לא", ...). The window is
            # deliberately tight so that a negation elsewhere in the
            # message can't mask a genuine crisis phrase elsewhere
            # ("I didn't mean it, I wanna die" must still trigger).
            # Check the joined phrase so multi-word negations like
            # "no longer" match too.
            before = t[: m.start()]
            words = re.findall(r"[a-z'\u0590-\u05FF]+", before)
            if not NEGATION_RE.search(" ".join(words[-2:])):
                if any(b.search(m.group(0)) for b in BARE_SUICIDE_RE) and \
                        rx.pattern in (r"\bsuicid\w*\b", r"\bsuisid\w*\b"):
                    continue  # bare suicide word: decide after the loop
                return True
    # Only a bare suicide word matched (un-negated). It fires unless the
    # message is plainly informational.
    for b in BARE_SUICIDE_RE:
        for m in b.finditer(t):
            before = t[: m.start()]
            words = re.findall(r"[a-z'\u0590-\u05FF]+", before)
            if NEGATION_RE.search(" ".join(words[-2:])):
                continue
            if INFORMATIONAL_RE.search(t) or INFO_QUERY_RE.search(t):
                return False
            return True
    return False


def is_informational_word_question(text):
    """True for clearly informational questions about the word 'suicide'
    itself (meaning/definition/spelling). Caller must still run the
    classifier fail-safe before taking the gentle path."""
    t = text.lower()
    return any(b.search(t) for b in BARE_SUICIDE_RE) and bool(
        INFO_QUERY_RE.search(t)
    )


def classify_risk(text):
    """Small classifier prompt — the second layer of the pre-model risk
    check. Catches indirect/coded phrasing the keyword list misses.
    Fails SAFE (keyword layer still applies) so a classifier outage can
    never block or falsely crisis-lock a conversation."""
    if os.environ.get("CRISIS_CLASSIFIER", "on").lower() == "off":
        return False
    try:
        out = call_anthropic(
            SUMMARY_MODEL,
            CLASSIFIER_SYSTEM,
            [{"role": "user", "content": text}],
            max_tokens=4,
        )
        return out.strip().upper().startswith("RISK")
    except Exception:
        return False


def assess_risk(text):
    """Full pre-model risk check: keyword layer first, classifier second.
    Runs BEFORE any normal model call, on every user message."""
    if detect_crisis_keywords(text):
        return True
    return classify_risk(text)


def detect_retraction(text):
    """True when the user clearly walks back a crisis statement."""
    return bool(RETRACTION_RE.search(text.lower()))


_KIN_EN = (
    r"mom|mother|dad|father|brother|sister|son|daughter|wife|husband|"
    r"grandma|grandmother|grandpa|grandfather|grandparent|friend|"
    r"partner|baby|child|dog|cat"
)
_GAP = r"(?:[\w']+\s+){0,2}?"
GRIEF_RE = [
    # "my <kin> died / passed away / was killed" — the death word
    # must be a real one, so "my dad passed his test" never fires,
    # and "almost/nearly died" is a near-miss, not a loss.
    re.compile(
        r"\bmy\s+" + _GAP + r"(?:" + _KIN_EN + r")\b[^.!?\n]{0,40}?\b"
        r"(?:(?<!almost )(?<!nearly )died|passed\s+away|was\s+killed|"
        r"lost\s+(?:his|her|their)\s+life)\b",
        re.I,
    ),
    # "<kin> (just) died / passed away" without the possessive.
    re.compile(
        r"\b(?:" + _KIN_EN + r")\s+(?:just\s+)?"
        r"(?:(?<!almost )(?<!nearly )died|passed\s+away)\b",
        re.I,
    ),
    # "I/we lost my/our <kin>" — but not "lost my friend's number"
    # (the possessive means an object was lost, not the person).
    re.compile(
        r"\b(?:lost|losing)\s+(?:my|our)\s+" + _GAP +
        r"(?:" + _KIN_EN + r")\b(?!\s*['’]s\b)",
        re.I,
    ),
    # "my <kin>'s funeral" / "his funeral" / "burying my brother".
    re.compile(
        r"\b(?:my|our|his|her|their)\s+" + _GAP + r"funeral\b", re.I),
    re.compile(
        r"\b(?:burying|buried)\s+(?:my|our)\s+" + _GAP +
        r"(?:" + _KIN_EN + r")\b",
        re.I,
    ),
    # Hebrew: kinship term + a death word (נפטר/מת/נהרג/הלך לעולמו),
    # the reversed "נפטרה לי אמא" shape, and "איבדתי את <kin>".
    re.compile(
        r"(?:ה)?(?:אמא|אבא|סבתא|סבא|אח שלי|אחות שלי|אחי|אחותי|בעלי|"
        r"אשתי|הבן שלי|הבת שלי|חבר שלי|חברה שלי)"
        r"[^.!?\n]{0,25}?"
        r"(?:נפטר|נפטרה|נפטרו|מת|מתה|נהרג|נהרגה|הלך לעולמו|הלכה לעולמה)"
    ),
    re.compile(r"(?:נפטר|נפטרה)\s+(?:לי\s+)?(?:ה)?(?:אמא|אבא|סבתא|סבא)"),
    re.compile(
        r"איבדתי\s+את\s+(?:ה)?(?:אמא|אבא|סבתא|סבא|אחי|אחותי|בעלי|אשתי)"
    ),
]


def detect_grief(text):
    """Deterministic bereavement detection (Tab persona test): a
    grief disclosure gets the fixed warm opening line above.
    Deliberately narrow — a person plus a death word — so ordinary
    talk ('I died laughing', 'my dad passed his test', 'נפטרתי
    מהעבודה') never fires."""
    t = text or ""
    return any(rx.search(t) for rx in GRIEF_RE)


HEBREW_RE = re.compile(r"[\u0590-\u05FF]")


def has_hebrew(text):
    """True when the message contains Hebrew characters."""
    return bool(HEBREW_RE.search(text))


def conversation_is_hebrew(hist_rows, msg):
    """True when the conversation is in Hebrew: the current message or
    any of the last few user messages contains Hebrew. Used so a Hebrew
    conversation stays fully Hebrew even if one follow-up is short or
    typed in transliteration."""
    if has_hebrew(msg):
        return True
    user_msgs = [r["content"] for r in hist_rows if r["role"] == "user"]
    return any(has_hebrew(c) for c in user_msgs[-3:])


def message_is_hebrew(msg):
    """Language of THIS message only. Crisis routing uses this —
    never conversation history — so an English crisis message right
    after a Hebrew one gets the English fixed reply, banner and 988,
    and vice versa (Tab round 3)."""
    return has_hebrew(msg)


def crisis_resource_line(hebrew, reply):
    """Deterministic crisis-resource footer, locale-aware.

    Hebrew conversation -> Israel's ERAN emotional-support hotline 1201
    is prominent. English -> make sure 988 is present (the model usually
    includes it via the crisis protocol; this is the deterministic
    backstop so the right number is always there).
    """
    if hebrew:
        if "1201" in reply:
            return ""
        return (
            "\n\nבישראל: קו הסיוע הנפשי של ער״ן — 1201 (זמין 24/7). "
            "בארה״ב: חייג או סמס 988."
        )
    if "988" not in reply:
        return (
            "\n\nIf you need help right now: in the US, call or text "
            "988 — free, any time, day or night."
        )
    return ""


def _today_str():
    """Today's date as the user lives it (America/New_York), e.g.
    'Sunday, October 25, 2026'. Falls back to UTC if tzdata is
    unavailable. The model must always know what day it is — without
    it, a remembered plan for 'Thursday' gets asked about as if it
    already happened (Tab's date bug)."""
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = datetime.now(timezone.utc)
    return now.strftime("%A, %B %d, %Y").replace(" 0", " ")


def _week_map_str():
    """The next 7 days with full dates ('Sunday, October 25, 2026;
    Monday, October 26, 2026; ...'), so relative-day references in a
    conversation ('on Thursday') resolve to the correct date instead
    of the memory model guessing — its first live guess landed two
    days off."""
    from datetime import timedelta

    try:
        from zoneinfo import ZoneInfo

        base = datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        base = datetime.now(timezone.utc).date()
    return "; ".join(
        (base + timedelta(days=i)).strftime("%A, %B %d, %Y").replace(" 0", " ")
        for i in range(7)
    )


def _fmt_date(ts):
    """'2026-10-25 14:03:11' -> 'October 25, 2026' ('' if unparseable)."""
    try:
        dt = datetime.strptime((ts or "")[:10], "%Y-%m-%d")
        return dt.strftime("%B %d, %Y").replace(" 0", " ")
    except Exception:
        return ""


DATE_RULES = (
    "DATES: Today is {today}. Before you ask about anything from "
    "memory, check its date against today. A remembered plan or event "
    "dated today or later has NOT happened yet — never ask how it "
    "went; at most, ask if they feel ready for it. If a remembered "
    "item carries no clear date, do not assume it already happened."
)

RETURN_NOTE = (
    "RETURNING VISIT: This person is coming back after a break — their "
    "previous message was a while ago, so this is a new visit, not a "
    "continuation of the same sitting. Open with ONE brief line that "
    "picks up where you left off — what they were working through, "
    "from the memory note and the messages above. Then follow their "
    "lead: if they ask a question or raise something new, answer that "
    "first — never hold their question hostage to a memory check-in. "
    "And mind the dates (see DATES): a remembered plan dated today or "
    "later hasn't happened yet — don't ask how it went."
)


def build_system(profile, summary, has_prior=False, returning=False,
                 summary_date=""):
    parts = [SYSTEM_PROMPT]
    parts.append(DATE_RULES.format(today=_today_str()))
    bits = []
    if profile.get("name"):
        bits.append("Name: %s" % profile["name"])
    if profile.get("age_range"):
        bits.append("Age range: %s" % profile["age_range"])
    if profile.get("notes"):
        bits.append("Intake notes: %s" % profile["notes"])
    if bits:
        parts.append("About this person:\n" + "\n".join(bits))
    if summary:
        label = "What you remember from earlier sessions"
        d = _fmt_date(summary_date)
        if d:
            label += " (memory note last updated %s)" % d
        parts.append(label + ":\n" + summary)
    elif has_prior:
        parts.append(
            "This person has spoken with you before — their earlier "
            "messages are in this conversation — but there is no memory "
            "note yet. Do NOT treat them as brand new and do NOT restart "
            "the first-session intake: greet them as someone returning, "
            "pick up the thread from the messages above, and continue "
            "naturally."
        )
    else:
        parts.append(
            "This is a new person — no earlier sessions. Follow the FIRST-SESSION INTAKE."
        )
    if returning:
        parts.append(RETURN_NOTE)
    return "\n\n---\n\n".join(parts)


def _norm_item(text):
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


# ---- Deleted-item matching (Tab fix) --------------------------------
# Tombstones used to match by exact normalized text only, so the
# memory model could resurrect a deleted theme simply by rewording
# it ("work projects accumulating; feeling perpetually behind" came
# back as "Manager continuously adds projects; feels perpetually
# behind even on weekends"). Matching is now by content tokens with a
# light stemmer, so a reworded equivalent is recognized and dropped.

_ITEM_TOKEN_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "they",
    "them", "their", "then", "than", "when", "what", "been", "were",
    "are", "was", "has", "have", "had", "not", "but", "all", "any",
    "about", "into", "over", "per", "very", "just", "even",
    "של", "על", "עם", "את", "הוא", "היא", "זה", "זו", "גם", "לא",
    "כל", "אני", "הם", "יש", "אין", "כי", "או", "אם", "מה", "לי",
}


def _stem_token(tok):
    if tok.isascii():
        for suf in ("ing", "edly", "ed", "es", "s"):
            if len(tok) > len(suf) + 2 and tok.endswith(suf):
                return tok[: -len(suf)]
    return tok


def _item_tokens(text):
    """Content-word tokens of a memory item (stemmed, stopwords out).
    Works for Hebrew items too — the tokenizer is unicode-aware; the
    stemmer only touches Latin words."""
    toks = set()
    for tok in re.findall(r"[^\W_]+", (text or "").lower(), re.UNICODE):
        if len(tok) < 3 or tok in _ITEM_TOKEN_STOPWORDS:
            continue
        toks.add(_stem_token(tok))
    return toks


def _token_overlap(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _tokens_match_deleted(cand, deleted):
    """True when candidate tokens restate a deleted item's tokens:
    a short deleted item must be fully restated; a longer one needs
    at least 3 shared content words covering 60% of the shorter set."""
    if not cand or not deleted:
        return False
    inter = cand & deleted
    if not inter:
        return False
    if len(deleted) <= 2:
        return deleted <= cand or cand <= deleted
    return len(inter) >= 3 and _token_overlap(cand, deleted) >= 0.6


# ---- Repeated-closer suppression (Tab fix) -------------------------
# Two replies in a row ended with the identical closer ("would you be
# open to trying…"). A reply whose closing sentence repeats the
# previous reply's closing sentence ships without it.

_RESOURCE_TAIL_RE = re.compile(
    r"(call or text 988|free, any time|741741|ער״ן|בארה״ב)", re.S)


def _closing_sentence(text):
    """The reply's last real sentence, skipping a trailing
    deterministic crisis-resource footer if one was appended."""
    parts = [p.strip() for p in
             re.split(r"(?<=[.!?…])\s+", (text or "").strip()) if p.strip()]
    while parts and _RESOURCE_TAIL_RE.search(parts[-1]):
        parts.pop()
    return parts[-1] if parts else ""


def _drop_repeated_closer(reply, prev_reply):
    cur = _closing_sentence(reply)
    prev = _closing_sentence(prev_reply)
    if not cur or not prev:
        return reply
    ct, pt = _item_tokens(cur), _item_tokens(prev)
    same = _norm_item(cur) == _norm_item(prev) or (
        len(ct & pt) >= 3 and _token_overlap(ct, pt) >= 0.85)
    if not same:
        return reply
    stripped = reply.strip()
    if not stripped.endswith(cur):
        return reply
    rest = stripped[: -len(cur)].strip()
    return rest if rest else reply


# ---------------- Output filter (Tab ruling, Oct 26) -----------------
# Prompt instructions alone did NOT keep banned identity words out of
# model replies, so this is enforced in CODE on every model reply —
# reflection chat, rehearse partner, and debrief — before the text is
# displayed or saved. Sentence-level: a sentence containing a banned
# word is removed whole, so grammar is never left broken mid-sentence.
# The one exception is the exact phrase "Not therapy" (the tagline's
# own disclaimer shape): a sentence whose only therapy-word is inside
# "not therapy" survives.
_BANNED_CHECK_RES = [
    re.compile(r"\bpsycholog\w*", re.I),
    re.compile(r"\bcounsell?ors?\b", re.I),
    re.compile(r"\bcounsell?ing\b", re.I),
    re.compile(r"\btreatments?\b", re.I),
    re.compile(r"\bdiagnos\w*", re.I),
    re.compile(r"\bheal(?:s|ed|ing|er|ers)?\b", re.I),
    re.compile(r"\bcur(?:e|es|ed|ing)\b", re.I),
]
_NOT_THERAPY_RE = re.compile(r"\bnot\s+therapy\b", re.I)
_THERAP_RE = re.compile(r"therap\w*", re.I)
_BANNED_HE = ("פסיכולוג", "טיפול", "מטפל")
_SENT_SEP_RE = re.compile(r"(?<=[.!?…])(\s+)")
_SCRUB_FALLBACK = "I'm here with you — tell me more about that."
_SCRUB_FALLBACK_HE = "אני כאן איתך — ספר לי עוד על זה."


def _sentence_banned(seg):
    """True when one sentence carries a banned identity word."""
    for w in _BANNED_HE:
        if w in seg:
            return True
    # "Not therapy" is the single allowed therapy-word shape: judge
    # the therap* family only after removing those occurrences.
    if _THERAP_RE.search(_NOT_THERAPY_RE.sub("", seg)):
        return True
    return any(rx.search(seg) for rx in _BANNED_CHECK_RES)


# Availability claims (Tab round 3): the model answered "Are you
# available 24/7?" with "Yes… any time of day or night, any day
# of the week". Solace makes NO availability claims about itself,
# ever — a sentence claiming one is removed like a banned-word
# sentence. Sentences about the HOTLINES (988 / 741741 / ער״ן
# 1201 / מד״א 101 / 911) are resource context, not a Solace
# claim — "free, confidential, 24/7" describing 988 is Tab's
# settled wording and is never touched.
_AVAILABILITY_CLAIM_RES = [
    re.compile(r"24\s*/\s*7"),
    re.compile(r"\baround the clock\b", re.I),
    re.compile(r"\bany time of day or night\b", re.I),
    re.compile(r"\bany day of the week\b", re.I),
    re.compile(r"\balways available\b", re.I),
    re.compile(r"\bavailable (?:at\s+)?(?:any|all) times?\b", re.I),
    re.compile(r"\bi'?m here (?:for you )?(?:day or night|"
               r"any ?time|whenever you)", re.I),
    re.compile(r"מסביב לשעון"),
    re.compile(r"זמי\w+\s+תמיד"),
    re.compile(r"תמיד\s+זמי\w+"),
    re.compile(r"אני כאן (?:בשבילך\s+)?(?:יום ולילה|בכל שעה)"),
]
_RESOURCE_CONTEXT_RE = re.compile(
    r"(988|741741|1201|ער״ן|מד״א|\b911\b|\b101\b|lifeline|hotline|"
    r"קו\s+(?:ה)?(?:סיוע|חיים))",
    re.I,
)


def _sentence_availability_claim(seg):
    """True when one sentence claims Solace is available around
    the clock (and is not talking about a crisis hotline)."""
    if _RESOURCE_CONTEXT_RE.search(seg):
        return False
    return any(rx.search(seg) for rx in _AVAILABILITY_CLAIM_RES)


def _sentence_segments(text):
    """Split into (sentence, separator-after) pairs, losslessly."""
    parts = _SENT_SEP_RE.split(text or "")
    segs = []
    for i in range(0, len(parts), 2):
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        segs.append((parts[i], sep))
    return segs


def _scrub_join(segs, include_last):
    """Join the unbanned segments. With include_last=False the final
    segment is held back unless it ends in sentence punctuation —
    the streaming form, so a banned word can never flash on screen
    inside a still-growing sentence."""
    out = []
    for idx, (seg, sep) in enumerate(segs):
        last = idx == len(segs) - 1
        if last and not include_last:
            tail = seg.rstrip().rstrip("\"'”’)]")
            if not tail or tail[-1] not in ".!?…":
                break
        if _sentence_banned(seg) or _sentence_availability_claim(seg):
            continue
        out.append(seg + sep)
    return "".join(out)


# Dangling opener (Tab round 3): when the filter removes the
# sentence that carried the refusal ("I can't be your
# therapist"), a model reply can be left OPENING with the
# fragment "What I *can* do…" — a sentence with its first half
# cut off. A reply that starts with that fragment loses the
# fragment sentence.
_DANGLING_OPENER_RE = re.compile(
    r"^\s*what\s+i\s+\*?can\*?\s+do\b", re.I)


def _drop_dangling_opener(text, keep_if_only=False):
    segs = _sentence_segments(text)
    real = [i for i, (s, _) in enumerate(segs) if s.strip()]
    if not real:
        return text
    first = real[0]
    if not _DANGLING_OPENER_RE.search(segs[first][0]):
        return text
    rest = "".join(s + sep for s, sep in segs[first + 1:]).strip()
    if not rest and keep_if_only:
        return text
    return rest


def _truncate_glued_reply(text):
    """One reply only (Tab round 3): a standalone divider line
    (--- / *** / ___) in a model reply is the seam where a second
    reply was glued onto the first — everything from the divider
    on is cut. Runs in scrub_reply / scrub_prefix, i.e. on every
    reply surface. (The grief BODY does not pass through here —
    it goes through scrub_field + _grief_clean_body, whose own
    seam handling is Tab's round-2 ruling: strip the model's
    glued condolence, keep its real continuation.)"""
    if not text:
        return text
    m = _DIVIDER_LINE_RE.search(text)
    if not m:
        return text
    return text[:m.start()].rstrip()


def scrub_reply(text):
    """The full-text filter applied to every finished model reply."""
    out = _scrub_join(
        _sentence_segments(_truncate_glued_reply(text)),
        include_last=True)
    out = _drop_dangling_opener(out.strip())
    if out:
        return out
    if has_hebrew(text or ""):
        return _SCRUB_FALLBACK_HE
    return _SCRUB_FALLBACK


def scrub_prefix(text):
    """Streaming form: the scrubbed text of all COMPLETE sentences
    so far — safe to emit as a delta; the tail is still growing."""
    return _drop_dangling_opener(
        _scrub_join(
            _sentence_segments(_truncate_glued_reply(text)),
            include_last=False))


def scrub_field(text):
    """Debrief-field form: like scrub_reply but may return "" so the
    caller can substitute its own neutral default."""
    return _scrub_join(
        _sentence_segments(text), include_last=True).strip()


_CONDOLENCE_LEAD_RE = re.compile(
    r"(אני (?:ממש )?מצטער|משתתף בצער|תנחומיי|שלא תדעו עוד צער|"
    r"i'?m so sorry|i am so sorry|sorry for your loss|"
    r"my condolences)",
    re.I,
)


def _strip_dup_condolence(body):
    """Grief turns open with the FIXED condolence line; a model
    continuation that ALSO opens with its own condolence reads as a
    second, glued-on reply (Tab's Hebrew grief bug). Drop EVERY
    leading condolence sentence — the live failure wrote a whole
    multi-sentence condolence paragraph of its own, and stripping
    only the first sentence left the rest of that second reply in
    place."""
    if not body:
        return body
    for _ in range(6):
        segs = _sentence_segments(body)
        if len(segs) < 2:
            return body
        if not _CONDOLENCE_LEAD_RE.search(segs[0][0]):
            return body
        rest = "".join(s + sep for s, sep in segs[1:]).strip()
        if not rest:
            return body
        body = rest
    return body


# A markdown rule line on its own — models use it to split a reply
# into two "messages"; in a chat bubble it is exactly the visible
# seam of the glued-replies bug.
_DIVIDER_LINE_RE = re.compile(
    r"(?m)^[ \t]*(?:-{3,}|\*{3,}|_{3,}|—{2,})[ \t]*$")


def _strip_opening_echo(body, opening):
    """If the model parroted the fixed opening back as the start of
    its continuation, drop the echoed sentences — the caller
    prepends the one true opening itself."""
    if not body or not opening:
        return body
    open_segs = [s.strip() for s, _ in _sentence_segments(opening)
                 if s.strip()]
    for want in open_segs:
        segs = _sentence_segments(body)
        if len(segs) < 2 or segs[0][0].strip() != want:
            break
        rest = "".join(s + sep for s, sep in segs[1:]).strip()
        if not rest:
            break
        body = rest
    return body


# A condolence phrased as if hearing the news for the first time
# ("sorry to HEAR about your loss", "מצטער על האובדן") — distinct
# from a mid-reply "I'm sorry you're going through this". One of
# these AFTER the body's opening sentences means a second reply
# was glued on (Tab round 3).
_SECOND_REPLY_LEAD_RE = re.compile(
    r"(אני (?:ממש )?מצטער (?:לשמוע|על האובדן)|משתתף בצער|"
    r"תנחומיי|שלא תדעו עוד צער|"
    r"i'?m so sorry (?:to hear|for your loss)|my condolences)",
    re.I,
)


def _cut_second_reply_block(body, opening):
    """Cut a glued-on second reply inside a grief continuation:
    a later paragraph — or a later sentence — that opens as if
    the conversation were starting again (a fresh condolence for
    the news, or a repeat of the fixed opening). Everything from
    there on is the second reply; it goes."""
    if not body:
        return body
    open_firsts = {
        s.strip() for s, _ in _sentence_segments(opening or "")
        if s.strip()}
    paras = body.split("\n\n")
    if len(paras) > 1:
        for i in range(1, len(paras)):
            head = paras[i].strip()
            if not head:
                continue
            first_sent = _sentence_segments(head)[0][0].strip()
            if _SECOND_REPLY_LEAD_RE.search(first_sent) \
                    or first_sent in open_firsts:
                return "\n\n".join(paras[:i]).strip()
    segs = _sentence_segments(body)
    for i in range(1, len(segs)):
        s = segs[i][0].strip()
        if s and (_SECOND_REPLY_LEAD_RE.search(s)
                  or s in open_firsts):
            return "".join(x + sep for x, sep in segs[:i]).strip()
    return body


# Hebrew gender consistency (Tab round 3): the model's grief
# continuation addressed the person as masculine in one half
# ("קח… שאתה צריך") and feminine in the other ("שאת מרגישה…
# תרצי"). The fixed opening addresses the person in masculine
# forms, so unambiguous feminine SECOND-PERSON forms in the
# continuation are rewritten to the masculine forms (forms about
# the person who died — היא, עליה, שלה — are never touched).
# Anything still carrying a feminine second-person marker after
# the rewrite loses its sentence: one reply, one gender.
_HE_FEM_VERB_MASC = {
    "מרגישה": "מרגיש", "צריכה": "צריך", "יכולה": "יכול",
    "חושבת": "חושב", "יודעת": "יודע", "נמצאת": "נמצא",
    "מסוגלת": "מסוגל", "אומרת": "אומר", "שואלת": "שואל",
    "כותבת": "כותב", "מדברת": "מדבר", "מבקשת": "מבקש",
    "מספרת": "מספר", "רוצה": "רוצה",
}
_HE_FEM_VERB_ALT = "|".join(_HE_FEM_VERB_MASC)
_HE_FEM2P_RE = re.compile(
    r"(שאת|את)\s+(?:" + _HE_FEM_VERB_ALT + r")")
_HE_FEM2P_LEFTOVER_RE = re.compile(
    r"(שאת\b|תרצי|תרגישי|תקחי|קחי\b|"
    r"את\s+(?:" + _HE_FEM_VERB_ALT + r"))")


def _he_gender_consistency(body):
    if not body or not _HE_FEM2P_LEFTOVER_RE.search(body):
        return body

    def _verb_sub(m):
        verb = m.group(0).split(None, 1)[1]
        return m.group(1) + " " + _HE_FEM_VERB_MASC[verb]

    out = _HE_FEM2P_RE.sub(_verb_sub, body)
    out = re.sub(r"תרצי", "תרצה", out)
    out = re.sub(r"תרגישי", "תרגיש", out)
    out = re.sub(r"תקחי", "תקח", out)
    out = re.sub(r"קחי\b", "קח", out)
    out = re.sub(r"ספרי\s+לי", "ספר לי", out)
    out = re.sub(r"שאת\b", "שאתה", out)
    segs = _sentence_segments(out)
    kept = [(s, sep) for s, sep in segs
            if not _HE_FEM2P_LEFTOVER_RE.search(s)]
    if len(kept) != len(segs):
        out = "".join(s + sep for s, sep in kept).strip()
    return out


def _grief_clean_body(body, opening):
    """The deterministic cleanup every grief continuation passes
    through (sync, stream-visible, saved text): no divider seams,
    no echoed opening, no leading condolence pile-up, no glued-on
    second reply, one consistent gender of address — so the
    final reply is the fixed opening + ONE continuation."""
    if not body:
        return body
    body = _DIVIDER_LINE_RE.sub("", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    body = _strip_opening_echo(body, opening)
    body = _strip_dup_condolence(body)
    body = _cut_second_reply_block(body, opening)
    if opening and has_hebrew(opening):
        body = _he_gender_consistency(body)
    body = body.strip()
    if opening and body and body in opening:
        # Nothing left but a fragment of the opening itself.
        return ""
    return body


def _grief_stream_visible(raw_text, opening, started):
    """The visible continuation during a grief STREAM: the scrubbed
    prefix, cleaned exactly like the final body. Held back ("")
    until two complete sentences exist — the condolence strip
    needs the second sentence to know the first was the model's
    own repeat, and emitting that first sentence live is what
    glued two replies together on screen."""
    scrubbed = scrub_prefix(raw_text)
    if not started:
        segs = [s for s, _ in _sentence_segments(scrubbed) if s.strip()]
        if len(segs) < 2:
            return "", False
    return _grief_clean_body(scrubbed, opening), True


def _same_theme_judge(deleted_text, cand_text):
    """Gray-band arbiter for the tombstone guard: when a candidate
    shares some content words with a deleted item but not enough for
    the deterministic match, the cheap model decides whether it is
    the same theme in different words. Fails OPEN (item allowed) on
    any error — the deterministic band above it is the hard guard."""
    try:
        out = call_anthropic(
            SUMMARY_MODEL,
            "You compare memory items for a support app.",
            [{"role": "user", "content": (
                "A person deleted this memory item and never wants it "
                "kept:\n\"" + deleted_text + "\"\n\n"
                "A new memory item is proposed:\n\"" + cand_text + "\"\n\n"
                "Is the new item about the same theme or concern as "
                "the deleted one, even if worded differently? "
                "Reply with exactly YES or NO."
            )}],
            max_tokens=4,
        )
        return (out or "").strip().upper().startswith("YES")
    except Exception:
        return False


def _deleted_and_not_restated(text, tomb_entries, user_msgs, judge=None):
    """Tab fix: a deleted theme must NEVER come back through a memory
    rewrite — in any wording. The one exception is the person
    themselves raising it again in a message written AFTER the
    deletion (tomb_entries carry the deletion time for that check;
    legacy tombstones without a timestamp accept any user message in
    the window as a possible restatement, matching the old behavior
    where only the exact wording was blocked).

    tomb_entries: [(deleted_text, deleted_tokens, created_at_str)]
    user_msgs:    [(created_at_str, message_tokens)] — user role only.
    judge: optional (deleted_text, cand_text) -> bool gray-band
    arbiter; production passes _same_theme_judge.
    """
    cand = _item_tokens(text)
    if not cand:
        return False
    for deleted_text, deleted, tcreated in tomb_entries:
        token_hit = _tokens_match_deleted(cand, deleted)
        # Gray band: shares >=2 content words with the deleted item
        # but not enough for the deterministic match — a hard
        # paraphrase the token layer alone can't judge.
        gray = not token_hit and len(cand & deleted) >= 2
        if not token_hit and not gray:
            continue
        for mcreated, mtoks in user_msgs:
            if tcreated and mcreated <= tcreated:
                continue  # said BEFORE the deletion — not a restatement
            if len(cand & mtoks) >= 2 and _token_overlap(cand, mtoks) >= 0.5:
                return False  # the person raised it again themselves
        if token_hit:
            return True
        if judge is not None and judge(deleted_text, text):
            return True
    return False


def _parse_summary_output(out):
    """Split the memory-model output into (note, items|None).

    The model is asked to end with an ITEMS: line carrying a JSON array
    of {"kind": "theme"|"goal", "text": ...}. Parsing is defensive: any
    failure leaves items=None (existing items stay untouched)."""
    if not out:
        return "", None
    idx = out.rfind("ITEMS:")
    if idx < 0:
        return out.strip(), None
    note = out[:idx].strip()
    raw = out[idx + len("ITEMS:"):]
    items = None
    try:
        a = raw.index("[")
        b = raw.rindex("]")
        data = json.loads(raw[a:b + 1])
        items = []
        for it in data[:10]:
            if isinstance(it, dict) and it.get("kind") in ("theme", "goal"):
                t = str(it.get("text", "")).strip()
                if t:
                    items.append({"kind": it["kind"], "text": t[:200]})
    except Exception:
        items = None
    return note, items


# A completed rehearsal produces ONLY its dated note in memory —
# never a goal (Tab fix). The updater still sometimes restates the
# rehearsal's topic as a to-do goal ("Have actual conversation with
# landlord Dave about fixing the heating"), because the real talk
# sounds like an agreed next step. These signatures let a code guard
# recognize goal items that originated from a rehearsal: they
# reference the rehearsal person (name or relationship word) AND
# share vocabulary with the rehearsal topic.

_TOPIC_STOPWORDS = {
    "about", "with", "talk", "talks", "talking", "discuss", "discussing",
    "conversation", "have", "having", "their", "that", "this", "from",
    "need", "want", "tell", "telling", "please", "really", "just",
    "into", "over", "under", "will", "would", "should", "could",
    "when", "what", "them", "then", "than", "been", "were", "your",
    "yours", "mine", "ours", "actual", "real",
}

_BUILTIN_PERSON_CUES = {
    "boss": {"boss"},
    "dad": {"dad", "father"},
    "mom": {"mom", "mother"},
    "partner": {"partner", "husband", "wife"},
    "friend": {"friend"},
}


def _rehearsal_signatures(rehearsal_rows):
    """(person name tokens, relationship cue words, topic keywords)
    for the user's recent debriefed rehearsals."""
    names, cues, keywords = set(), set(), set()
    for rr in rehearsal_rows or []:
        for tok in re.findall(r"[a-z']+", (rr["person_name"] or "").lower()):
            if len(tok) >= 3:
                names.add(tok)
        for tok in re.findall(r"[a-z']+", (rr["relationship"] or "").lower()):
            if len(tok) >= 4:
                cues.add(tok)
        cues |= _BUILTIN_PERSON_CUES.get(rr["person_type"] or "", set())
        for tok in re.findall(r"[a-z']{4,}", (rr["topic"] or "").lower()):
            if tok not in _TOPIC_STOPWORDS:
                keywords.add(tok)
    return names, cues, keywords


def _guarded_item_kind(kind, text, sigs):
    """Final kind to store for one memory item, or None to drop it.
    Practice-worded items become themes (as before); a goal that
    references a rehearsal person AND shares its topic vocabulary is
    the rehearsal's own topic restated as a to-do — dropped, because
    the dated note already covers the rehearsal. Genuine user-stated
    goals pass through untouched."""
    if kind != "goal":
        return kind
    if re.search(r"rehears|practiced|practice (talk|conversation)",
                 text, re.I):
        return "theme"
    names, cues, keywords = sigs
    if not (names or cues) or not keywords:
        return kind
    toks = set(re.findall(r"[a-z']{3,}", (text or "").lower()))
    if (toks & names or toks & cues) and toks & keywords:
        return None
    return kind


def update_summary(user_id):
    """Background job: refresh the rolling memory note (cheap model) AND
    the structured memory items (themes/goals) the person can review in
    the "What Solace remembers about you" view. Items the person deleted
    are tombstoned and never re-added."""
    try:
        with db_lock:
            conn = _conn()
            rows = conn.execute(
                "SELECT role, content, created_at FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 40",
                (user_id,),
            ).fetchall()
            old = conn.execute(
                "SELECT summary FROM summaries WHERE user_id=?", (user_id,)
            ).fetchone()
            rehearsal_rows = conn.execute(
                "SELECT person_type, person_name, relationship, topic, created_at "
                "FROM rehearsals WHERE user_id=? AND state='debriefed' "
                "ORDER BY id DESC LIMIT 3",
                (user_id,),
            ).fetchall()
            tomb_rows = conn.execute(
                "SELECT norm, created_at FROM memory_tombstones WHERE user_id=?",
                (user_id,),
            ).fetchall()
            conn.close()
        # Deleted items (Tab fix): matched semantically at write time
        # below, and named to the updater here so the prose note does
        # not quietly resurrect them either.
        tomb_entries = [
            (r["norm"], _item_tokens(r["norm"]), r["created_at"] or "")
            for r in tomb_rows
        ]
        user_msgs = [
            (r["created_at"] or "", _item_tokens(r["content"]))
            for r in rows if r["role"] == "user"
        ]
        deleted_note = ""
        if tomb_rows:
            deleted_note = (
                "\n\nThe person has DELETED the following from their "
                "memory and does not want them kept: "
                + "; ".join(r["norm"] for r in tomb_rows)
                + ". Do NOT include these — or anything equivalent in "
                "different words — in the note or the ITEMS."
            )
        transcript = "\n".join(
            "%s: %s" % (r["role"], r["content"]) for r in reversed(rows)
        )
        # Completed rehearsals feed memory through this same updater:
        # one describing-language line each, so a finished practice
        # shows up as a theme/goal item the person can review. (They
        # are re-fed on every refresh, so the item survives refreshes.)
        rehearsal_note = ""
        if rehearsal_rows:
            lines = []
            for rr in reversed(rehearsal_rows):
                label = _rehearsal_person_label(rr)
                topic = (rr["topic"] or "").strip()
                when = _fmt_date(rr["created_at"])
                lines.append(
                    "The person rehearsed a hard talk with %s%s%s."
                    % (label, (" about " + topic) if topic else "",
                       (" (rehearsed on " + when + ")") if when else "")
                )
            rehearsal_note = (
                "\n\nRecent practice conversations to fold into the "
                "note and the ITEMS (describing language only). Each "
                "practice conversation is something the person has "
                "already DONE, so in ITEMS it is ALWAYS a \"theme\" — "
                "a short past-tense, dated note of what happened, "
                "e.g. {\"kind\": \"theme\", \"text\": \"Practiced the "
                "talk with their boss on October 25, 2026\"} — NEVER "
                "a \"goal\":\n"
                + "\n".join(lines)
                + "\nA practice conversation creates ONLY that one "
                "dated theme item — never add any other item about "
                "the practice or its topic, and in particular never "
                "a goal like \"have the actual conversation\": the "
                "real talk the person rehearsed is not a goal item "
                "unless the person stated it as their own goal in "
                "the conversation itself."
            )
        prompt = (
            "Today is %s.\n\n"
            "You are updating a private memory note for an AI support companion. "
            "Read the recent conversation and write (or update) a concise note covering: "
            "presenting concerns, key facts about the person, what seems to be helping, "
            "any practical exercises or homework assigned and whether the person tried them, "
            "and the current focus. Describing language only — describe, never label. "
            "Keep it under 200 words. If a previous note exists, merge the new information into it. "
            "DATING: when the person mentions a plan or event tied to a day or date "
            "('on Thursday', 'tomorrow'), write it WITH its full date — e.g. 'plans to "
            "talk with Dave on Thursday, October 29, 2026' — resolving relative days "
            "against today's date above, so a future plan is never later mistaken for "
            "something that already happened. Keep full dates already recorded. "
            "The coming days are: %s.\n\n"
            "After the note, add one final line that begins with ITEMS: followed by a JSON array "
            "of the memory items the person should be able to review, each an object "
            "{\"kind\": \"theme\" or \"goal\", \"text\": \"a short plain-language phrase\"}. "
            "Themes are what has been weighing on them and the topics they keep coming back to. "
            "Goals are what they are working toward, including any small step they agreed to try — "
            "but count something as a goal ONLY if the person stated it as their own aim in the "
            "conversation; never infer a goal from the topic of a practice conversation. "
            "Something the person has already DONE — including a practice conversation they completed — "
            "is a theme (a dated note of what happened), never a goal. "
            "Describing language only — never condition names or labels. At most 10 items. "
            "If there is nothing solid yet, end with ITEMS: [].\n\n"
            "Previous note:\n%s\n\nRecent conversation:\n%s%s"
            % (_today_str(), _week_map_str(),
               old["summary"] if old else "(none)",
               transcript, rehearsal_note + deleted_note)
        )
        out = call_anthropic(
            SUMMARY_MODEL,
            "You write brief private memory notes for a support companion.",
            [{"role": "user", "content": prompt}],
            max_tokens=640,
        )
        new_summary, items = _parse_summary_output(out)
        if not new_summary:
            new_summary = out.strip()
        with db_lock:
            conn = _conn()
            conn.execute(
                "INSERT INTO summaries (user_id, summary, updated_at) VALUES (?,?,datetime('now')) "
                "ON CONFLICT(user_id) DO UPDATE SET summary=excluded.summary, updated_at=datetime('now')",
                (user_id, new_summary),
            )
            if items is not None:
                conn.execute(
                    "DELETE FROM memory_items WHERE user_id=?", (user_id,)
                )
                sigs = _rehearsal_signatures(rehearsal_rows)
                for it in items:
                    if _deleted_and_not_restated(it["text"], tomb_entries,
                                                 user_msgs,
                                                 judge=_same_theme_judge):
                        continue  # deleted by the person — in ANY wording
                    # A completed practice conversation is a dated
                    # note of something that happened — a theme,
                    # never a goal (Tab fix), whatever kind the
                    # updater model assigned it; and a goal that is
                    # just the rehearsal's topic restated as a to-do
                    # is dropped entirely (see _guarded_item_kind).
                    kind = _guarded_item_kind(it["kind"], it["text"], sigs)
                    if kind is None:
                        continue
                    conn.execute(
                        "INSERT INTO memory_items (user_id, kind, text) VALUES (?,?,?)",
                        (user_id, kind, it["text"]),
                    )
            conn.commit()
            conn.close()
    except Exception:
        pass  # memory update must never break chat


# ------------------------------------------------------------ API ----
class ProfileIn(BaseModel):
    user_id: str = ""
    name: str = ""
    age_range: str = ""
    notes: str = ""


class ChatIn(BaseModel):
    user_id: str
    message: str


class DeleteIn(BaseModel):
    user_id: str


class MemoryItemDeleteIn(BaseModel):
    user_id: str
    item_id: int


UID_COOKIE = "solace_uid"


def _set_uid_cookie(response: Response, uid: str):
    """Long-lived first-party identity cookie — the backup for
    localStorage, which browsers evict or partition (in-app browsers,
    private tabs, storage cleanup). Device-level identity only."""
    response.set_cookie(
        UID_COOKIE, uid, max_age=60 * 60 * 24 * 365, path="/", samesite="lax"
    )


@asynccontextmanager
async def lifespan(app):
    init_db()
    yield


app = FastAPI(
    title="Solace",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.post("/api/profile")
def save_profile(body: ProfileIn, response: Response):
    uid = body.user_id.strip() or uuid.uuid4().hex[:16]
    name = body.name.strip()
    notes = body.notes.strip()
    with db_lock:
        conn = _conn()
        existing = conn.execute(
            "SELECT name, notes FROM users WHERE id=?", (uid,)
        ).fetchone()
        if existing:
            # A returning person who lands on the intake form again
            # (e.g. their history failed to load) must not clobber the
            # profile already stored: keep the original intake notes,
            # and never blank out a stored name.
            has_msgs = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE user_id=?", (uid,)
            ).fetchone()["c"]
            if has_msgs and existing["notes"]:
                notes = existing["notes"]
            if not name:
                name = existing["name"] or ""
        conn.execute(
            "INSERT INTO users (id, name, age_range, notes) VALUES (?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, age_range=excluded.age_range, "
            "notes=excluded.notes",
            (uid, name, body.age_range.strip(), notes),
        )
        conn.commit()
        conn.close()
    _set_uid_cookie(response, uid)
    return {"user_id": uid}


def _save_assistant_msg(uid, text):
    with db_lock:
        c = _conn()
        c.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?,?,?)",
            (uid, "assistant", text),
        )
        c.commit()
        c.close()


def _set_user_state(uid, crisis_mode=None, stepdown_v=None, banner_v=None,
                    lang_v=None):
    sets, vals = [], []
    if crisis_mode is not None:
        sets.append("crisis_mode=?")
        vals.append(1 if crisis_mode else 0)
    if stepdown_v is not None:
        sets.append("crisis_stepdown=?")
        vals.append(1 if stepdown_v else 0)
    if banner_v is not None:
        sets.append("crisis_banner=?")
        vals.append(1 if banner_v else 0)
    if lang_v is not None:
        sets.append("crisis_lang=?")
        vals.append(lang_v)
    if not sets:
        return
    vals.append(uid)
    with db_lock:
        c = _conn()
        c.execute("UPDATE users SET %s WHERE id=?" % ", ".join(sets), vals)
        c.commit()
        c.close()


def _prepare_turn(uid, msg):
    """Load state, record the user message, run the pre-model risk
    check, and decide the turn — BEFORE any model call, exactly as the
    original /api/chat pipeline did.

    Returns (ctx, outcome): outcome is ("final", payload) for the
    fixed-response paths, or ("model", mode, system) with mode in
    {"normal", "crisis", "info"} when a model reply is needed. The
    safety path therefore never streams through the model: a crisis
    hit is decided here and returned as a fixed final payload.
    """
    with db_lock:
        conn = _conn()
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            conn.execute("INSERT INTO users (id) VALUES (?)", (uid,))
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        profile = dict(row)
        conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?,?,?)",
            (uid, "user", msg),
        )
        conn.commit()
        srow = conn.execute(
            "SELECT summary, updated_at FROM summaries WHERE user_id=?", (uid,)
        ).fetchone()
        summary = srow["summary"] if srow else ""
        summary_date = srow["updated_at"] if srow else ""
        hist = conn.execute(
            "SELECT role, content, created_at FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 20",
            (uid,),
        ).fetchall()
        user_count = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE user_id=? AND role='user'",
            (uid,),
        ).fetchone()["c"]
        conn.close()

    # ---- Pre-model risk check (Solace Fix #1) ----
    # Runs BEFORE any normal model call, on every user message:
    # keyword layer first, then the classifier fail-safe.
    hebrew = conversation_is_hebrew(hist, msg)
    hotline = "1201" if hebrew else "988"
    # Crisis safety language is per-message, independent of history:
    # the fixed reply, the banner and latched follow-up safety text
    # all follow the language of the message that triggered them.
    safety_hebrew = message_is_hebrew(msg)
    safety_lang = "he" if safety_hebrew else "en"
    safety_hotline = "1201" if safety_hebrew else "988"
    kw_risk = detect_crisis_keywords(msg)
    emergency = detect_overdose(msg)
    info_q = (not kw_risk) and is_informational_word_question(msg)
    if kw_risk:
        risk = True
    elif emergency:
        # The emergency branch below returns before risk is ever
        # consulted — and NO model call may precede the 911 /
        # מד״א 101 reply (Tab round 3: the crisis reply must not
        # wait behind model work).
        risk = False
    else:
        # The classifier also runs for informational word questions, so
        # anything ambiguous or first-person still escalates; only a
        # clearly informational question the classifier clears takes
        # the gentle path (re-test fix #7).
        risk = classify_risk(msg)
    in_crisis_mode = bool(profile.get("crisis_mode"))
    stepdown = bool(profile.get("crisis_stepdown"))
    banner = bool(profile.get("crisis_banner"))
    # Banner language follows the current message whenever a crisis
    # is active or a fresh signal arrives; the stored value is only
    # the fallback for ordinary turns with the banner still up.
    banner_lang = profile.get("crisis_lang") or "en"
    if in_crisis_mode or banner:
        banner_lang = safety_lang
    # Explicit retraction with no active risk signal is the ONLY exit
    # path from crisis mode — and it is a two-step step-down now
    # (gentle check-in first, one calm exchange after), never an
    # instant exit (re-test fix #2).
    retracted = detect_retraction(msg) and not risk
    messages = [{"role": r["role"], "content": r["content"]} for r in reversed(hist)]

    # A new visit after a real break (previous message hours old) gets
    # an explicit welcome-back instruction in the normal flow.
    returning = False
    if len(hist) >= 2:
        try:
            prev_at = datetime.strptime(
                hist[1]["created_at"], "%Y-%m-%d %H:%M:%S"
            )
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            returning = (now - prev_at).total_seconds() >= 3 * 3600
        except Exception:
            returning = False

    ctx = {
        "uid": uid, "profile": profile, "summary": summary,
        "summary_date": summary_date,
        "messages": messages, "hebrew": hebrew, "hotline": hotline,
        "safety_hebrew": safety_hebrew, "safety_lang": safety_lang,
        "safety_hotline": safety_hotline,
        "banner": banner, "banner_lang": banner_lang,
        "user_count": user_count, "retracted": retracted,
        "returning": returning, "fallback_text": "",
        "need_professional": detect_need_professional(msg),
    }

    # Completed overdose (Tab ruling): medical emergency FIRST —
    # lead with 911 / מד״א 101, not the suicide question. Same latch
    # and banner as the crisis path; a separate deterministic
    # response, the frozen strings untouched.
    if emergency:
        _set_user_state(uid, crisis_mode=True, stepdown_v=False,
                        banner_v=True, lang_v=safety_lang)
        base = (EMERGENCY_RESPONSE_HE if safety_hebrew
                else EMERGENCY_RESPONSE)
        reply = base + crisis_resource_line(safety_hebrew, base)
        _save_assistant_msg(uid, reply)
        return ctx, ("final", {"reply": reply, "crisis": True,
                               "hotline": safety_hotline, "banner": True,
                               "banner_lang": safety_lang})

    # Fresh risk signal: fixed human response, no normal reply, latch
    # on, step-down reset, banner on for the rest of the session. Fully
    # Hebrew for Hebrew conversations (re-test fix #8).
    if risk:
        banner_lang = safety_lang
        _set_user_state(uid, crisis_mode=True, stepdown_v=False,
                        banner_v=True, lang_v=banner_lang)
        spouse = detect_spouse_loss(msg)
        if spouse:
            # Widow route (Tab ruling): the reply names the loss;
            # the frozen reply serves every other crisis.
            base = spouse_crisis_response(spouse, safety_hebrew)
        else:
            base = (FIXED_CRISIS_RESPONSE_HE if safety_hebrew
                    else FIXED_CRISIS_RESPONSE)
        reply = base + crisis_resource_line(safety_hebrew, base)
        _save_assistant_msg(uid, reply)
        return ctx, ("final", {"reply": reply, "crisis": True,
                               "hotline": safety_hotline, "banner": True,
                               "banner_lang": banner_lang})

    # Direct identity question ("Is this therapy?" / "Are you a
    # psychologist?" / Hebrew equivalents) and not in crisis mode:
    # a plain deterministic "No" (Tab ruling) — no model call, so
    # nothing for the word filter to mangle.
    identity_q = detect_identity_question(msg)
    if identity_q and not in_crisis_mode:
        reply = IDENTITY_ANSWERS[(identity_q, safety_hebrew)]
        _save_assistant_msg(uid, reply)
        return ctx, ("final", {"reply": reply, "crisis": False,
                               "hotline": hotline, "banner": banner,
                               "banner_lang": banner_lang})

    # Availability question ("Are you available 24/7?"): a
    # deterministic non-claim (Tab round 3) — no model call, so
    # no "any time of day or night" answer can be produced.
    if detect_availability_question(msg) and not in_crisis_mode:
        reply = (AVAILABILITY_ANSWER_HE if safety_hebrew
                 else AVAILABILITY_ANSWER)
        _save_assistant_msg(uid, reply)
        return ctx, ("final", {"reply": reply, "crisis": False,
                               "hotline": hotline, "banner": banner,
                               "banner_lang": banner_lang})

    # Clearly informational word question (and not in crisis mode):
    # answer it, with the gentle check-in — never the crisis script.
    if info_q and not in_crisis_mode:
        ctx["fallback_text"] = INFO_FALLBACK_HE if hebrew else INFO_FALLBACK
        return ctx, ("model", "info", INFO_SYSTEM_PROMPT)

    # Crisis mode latched from an earlier message.
    if in_crisis_mode:
        if stepdown:
            # The gentle check-in already went out; this is the next
            # exchange. Risk is False here (a fresh risk signal was
            # handled above and re-latches fully), so a calm, non-risk
            # exchange completes the step-down: normal conversation
            # resumes, but the banner flag stays on for the rest of
            # the session.
            _set_user_state(uid, crisis_mode=False, stepdown_v=False,
                            lang_v=safety_lang)
            ctx["banner_lang"] = safety_lang
            # fall through to the normal flow below
        elif retracted:
            # First walk-back: do NOT unlatch. One gentle check-in,
            # no relief tone, still in crisis mode, banner stays —
            # all in THIS message's language.
            _set_user_state(uid, stepdown_v=True, lang_v=safety_lang)
            reply = (
                RETRACTION_CHECKIN_RESPONSE_HE if safety_hebrew
                else RETRACTION_CHECKIN_RESPONSE
            )
            reply += crisis_resource_line(safety_hebrew, reply)
            _save_assistant_msg(uid, reply)
            return ctx, ("final", {"reply": reply, "crisis": True,
                                   "hotline": safety_hotline,
                                   "banner": True,
                                   "banner_lang": safety_lang})
        else:
            # Latched follow-up: the safety framing follows this
            # message's language, not the original crisis's.
            _set_user_state(uid, lang_v=safety_lang)
            ctx["banner_lang"] = safety_lang
            system = CRISIS_SYSTEM_PROMPT
            if profile.get("name"):
                system += "\n\nThe person's name is %s." % profile["name"]
            ctx["fallback_text"] = (
                "אני עדיין כאן איתך. אתה בטוח עכשיו? בישראל אפשר "
                "להתקשר לער״ן במספר 1201 בכל שעה. בארה״ב אפשר "
                "להתקשר או לשלוח הודעה ל־988, או לשלוח HOME ל־741741."
                if safety_hebrew else
                "I'm still here with you. Are you safe right now? "
                "Please reach out — call or text 988 any time, or text "
                "HOME to 741741."
            )
            return ctx, ("model", "crisis", system)

    # Grief disclosure (Tab persona test): not risk, not latched
    # crisis — the normal conversation, but the reply opens with
    # the fixed condolence line (prepended in _finish_model_turn)
    # and the model is told to stay with the grief.
    if detect_grief(msg):
        gsys = build_system(
            profile, summary,
            has_prior=user_count > 1, returning=returning,
            summary_date=summary_date,
        ) + GRIEF_NOTE
        if hebrew:
            gsys += _GRIEF_GENDER_NOTES[detect_grief_kin_he(msg)]
        ctx["grief_hebrew"] = hebrew
        ctx["grief_opening"] = grief_opening_for(hebrew, msg)
        ctx["fallback_text"] = (
            (ctx["grief_opening"] + " " + GRIEF_FALLBACK_HE) if hebrew
            else (GRIEF_OPENING + " " + GRIEF_FALLBACK)
        )
        return ctx, ("model", "grief", gsys)

    system = build_system(
        profile, summary,
        has_prior=user_count > 1, returning=returning,
        summary_date=summary_date,
    )
    return ctx, ("model", "normal", system)


def _finish_model_turn(ctx, mode, reply, background):
    """Post-process a model reply exactly as the original pipeline did
    (strip a [CRISIS] marker, latch on it in the normal flow, append
    the deterministic resource line where the original did, save the
    message, schedule the memory update) and build the payload."""
    uid = ctx["uid"]
    hebrew = ctx["hebrew"]
    hotline = ctx["hotline"]
    banner = ctx["banner"]
    banner_lang = ctx["banner_lang"]
    # Safety framing (resource line, banner, hotline) always follows
    # the language of the message that prompted this turn.
    safety_hebrew = ctx.get("safety_hebrew", hebrew)
    safety_lang = ctx.get("safety_lang", banner_lang)
    safety_hotline = ctx.get("safety_hotline", hotline)

    if mode == "info":
        if reply.startswith("[CRISIS]"):
            reply = reply[len("[CRISIS]"):].strip()
        reply = scrub_reply(reply)
        if ctx.get("need_professional"):
            reply = _ensure_referral(reply, safety_hebrew)
        _save_assistant_msg(uid, reply)
        return {"reply": reply, "crisis": False, "hotline": hotline,
                "banner": banner, "banner_lang": banner_lang}

    if mode == "grief" and reply.startswith("[CRISIS]"):
        # The model itself escalated a grief turn — hand it to the
        # normal path below, whose marker handling latches crisis
        # exactly as for any other turn.
        mode = "normal"
    if mode == "grief":
        # Prepend the FIXED opening line (never trust the model to
        # produce it, never let it be paraphrased away). The body is
        # cleaned deterministically — divider seams, an echoed
        # opening and the model's own condolence pile-up all come
        # out — so what remains is opening + ONE continuation. (The
        # old "the body already starts with the opening, keep it"
        # branch trusted a model echo, gender and all; gone.) No
        # crisis latch, no banner change, no hotline footer — grief
        # is not the crisis flow.
        opening = ctx.get("grief_opening") or (
            GRIEF_OPENING_HE if ctx.get("grief_hebrew")
            else GRIEF_OPENING)
        body = _grief_clean_body(scrub_field(reply.strip()), opening)
        reply = opening + ("\n\n" + body if body else "")
        _save_assistant_msg(uid, reply)
        if ctx["user_count"] == 3 or ctx["user_count"] % 5 == 0:
            background.add_task(update_summary, uid)
        return {"reply": reply, "crisis": False, "hotline": hotline,
                "banner": banner, "banner_lang": banner_lang}

    if mode == "crisis":
        if reply.startswith("[CRISIS]"):
            reply = reply[len("[CRISIS]"):].strip()
        reply = scrub_reply(reply)
        reply += crisis_resource_line(safety_hebrew, reply)
        _save_assistant_msg(uid, reply)
        return {"reply": reply, "crisis": True,
                "hotline": safety_hotline,
                "banner": True, "banner_lang": safety_lang}

    # normal
    crisis = False
    if reply.startswith("[CRISIS]"):
        reply = reply[len("[CRISIS]"):].strip()
        if not ctx["retracted"]:
            crisis = True
            banner = True
            banner_lang = safety_lang
            _set_user_state(uid, crisis_mode=True, stepdown_v=False,
                            banner_v=True, lang_v=banner_lang)
    reply = scrub_reply(reply)
    # Never end two replies in a row with the same closing line
    # (Tab fix) — compare against the previous stored reply.
    with db_lock:
        conn = _conn()
        prow = conn.execute(
            "SELECT content FROM messages WHERE user_id=? AND role='assistant' "
            "ORDER BY id DESC LIMIT 1",
            (uid,),
        ).fetchone()
        conn.close()
    if prow:
        reply = _drop_repeated_closer(reply, prow["content"])
    if ctx.get("need_professional"):
        # "Do I need a professional?" always carries the approved
        # referral line (Tab round 3) — appended only if the
        # model's own reply didn't already include it.
        reply = _ensure_referral(reply, safety_hebrew)
    if crisis:
        reply += crisis_resource_line(safety_hebrew, reply)
    _save_assistant_msg(uid, reply)
    # Memory note + reviewable items: build one early (3rd user
    # message) so a returning visitor is actually remembered, then
    # refresh every 5. (It used to update only every 10th message,
    # which short trial conversations never reached.)
    if ctx["user_count"] == 3 or ctx["user_count"] % 5 == 0:
        background.add_task(update_summary, uid)
    return {"reply": reply, "crisis": crisis,
            "hotline": safety_hotline if crisis else hotline,
            "banner": banner, "banner_lang": banner_lang}


def _chat_payload(uid, msg, background):
    uid = (uid or "").strip()
    msg = (msg or "").strip()
    if not uid:
        return {"reply": "Something went wrong starting your session. Please refresh and try again.", "crisis": False, "banner": False, "banner_lang": "en"}
    if not msg:
        return {"reply": "I'm here — what's on your mind?", "crisis": False, "banner": False, "banner_lang": "en"}

    ctx, outcome = _prepare_turn(uid, msg)
    if outcome[0] == "final":
        return outcome[1]
    _, mode, system = outcome
    try:
        reply = call_anthropic(CHAT_MODEL, system, ctx["messages"])
    except Exception:
        if mode == "normal":
            return {"reply": "I'm having trouble connecting right now. Please try again in a moment — I'm still here.", "crisis": False, "hotline": ctx["hotline"], "banner": ctx["banner"], "banner_lang": ctx["banner_lang"]}
        reply = ctx["fallback_text"]
    return _finish_model_turn(ctx, mode, reply, background)


def _fast_final_turn(uid, msg):
    """Resolve a turn that pure-regex checks decide completely —
    a keyword crisis or a completed overdose — with NO model
    call anywhere in its path, or return None when the turn
    needs the model. The async chat endpoints call this FIRST,
    in the event loop, so the frozen crisis / emergency reply is
    produced the moment the request arrives and never queues
    behind other users' model calls (Tab round 3: with 10 users
    at once the 988 reply took 2–5 s). _prepare_turn is only
    regex + local SQLite on this path — milliseconds."""
    if not (detect_crisis_keywords(msg) or detect_overdose(msg)):
        return None
    _, outcome = _prepare_turn(uid, msg)
    if outcome[0] == "final":
        return outcome[1]
    return None


@app.post("/api/chat")
async def chat(body: ChatIn, background: BackgroundTasks,
               response: Response):
    if body.user_id.strip():
        _set_uid_cookie(response, body.user_id.strip())
    uid = body.user_id.strip()
    msg = body.message.strip()
    if uid and msg:
        fast = _fast_final_turn(uid, msg)
        if fast is not None:
            return fast
    return await run_in_threadpool(
        _chat_payload, body.user_id, body.message, background)


def _anthropic_stream_chunks(system, messages):
    """Yield raw reply text chunks from the Anthropic API (streaming).
    Local-dev fallback (no API key): one chunk via call_anthropic."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        yield call_anthropic(CHAT_MODEL, system, messages)
        return
    payload = {
        "model": CHAT_MODEL,
        "system": system,
        "messages": messages,
        "max_tokens": 1024,
        "stream": True,
    }
    with httpx.Client(timeout=120) as client:
        with client.stream(
            "POST",
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    evt = json.loads(data)
                except Exception:
                    continue
                if evt.get("type") == "content_block_delta":
                    text = evt.get("delta", {}).get("text", "")
                    if text:
                        yield text


def _sse(obj):
    return "data: " + json.dumps(obj) + "\n\n"


def _chat_stream_events(uid, msg, background):
    """SSE event generator for /api/chat/stream. The pre-model risk
    check (_prepare_turn) runs first: fixed-response paths emit their
    frozen text as ONE delta, immediately — they never stream through
    the model. Only decided model turns stream, and a [CRISIS] marker
    at the very start is held back (never emitted) until it can be
    stripped, mirroring the non-streaming post-processing."""
    uid = (uid or "").strip()
    msg = (msg or "").strip()
    if not uid:
        payload = {"reply": "Something went wrong starting your session. Please refresh and try again.", "crisis": False, "banner": False, "banner_lang": "en"}
        yield _sse({"type": "delta", "text": payload["reply"]})
        yield _sse({"type": "done", **payload})
        return
    if not msg:
        payload = {"reply": "I'm here — what's on your mind?", "crisis": False, "banner": False, "banner_lang": "en"}
        yield _sse({"type": "delta", "text": payload["reply"]})
        yield _sse({"type": "done", **payload})
        return

    ctx, outcome = _prepare_turn(uid, msg)
    if outcome[0] == "final":
        payload = outcome[1]
        yield _sse({"type": "delta", "text": payload["reply"]})
        yield _sse({"type": "done", **payload})
        return
    _, mode, system = outcome

    if mode == "grief":
        # The fixed condolence opens the reply — send it at once,
        # before the model's continuation streams. (The done
        # payload stays authoritative, as everywhere else.)
        opening = ctx.get("grief_opening") or (
            GRIEF_OPENING_HE if ctx.get("grief_hebrew")
            else GRIEF_OPENING)
        yield _sse({"type": "delta", "text": opening + "\n\n"})

    raw = ""
    skip = None  # None until the [CRISIS]-marker question is settled
    emitted_visible = ""
    failed = False
    grief_started = False
    grief_frozen = False
    try:
        for chunk in _anthropic_stream_chunks(system, ctx["messages"]):
            raw += chunk
            if skip is None:
                if raw.startswith("[CRISIS]"):
                    skip = len("[CRISIS]")
                elif len(raw) >= len("[CRISIS]"):
                    skip = 0
                else:
                    continue
            # Leading whitespace is stripped once, in effect: raw only
            # grows at the end, so raw[skip:].lstrip() is a stable
            # prefix across chunks (the final text is stripped anyway).
            # The output filter runs here too: only scrubbed COMPLETE
            # sentences are emitted, so a banned word can never reach
            # the screen even briefly inside a growing sentence.
            if mode == "grief":
                # Grief streams show the CLEANED body (dividers,
                # echoes and the model's own condolence removed),
                # held until two sentences exist. If cleaning ever
                # rewrites text already emitted, freeze — the done
                # payload stays authoritative and replaces the
                # bubble, exactly as elsewhere.
                visible, grief_started = _grief_stream_visible(
                    raw[skip:].lstrip(), opening, grief_started)
                if grief_frozen or not visible.startswith(
                        emitted_visible):
                    grief_frozen = True
                    continue
            else:
                visible = scrub_prefix(raw[skip:].lstrip())
            if len(visible) > len(emitted_visible):
                new = visible[len(emitted_visible):]
                emitted_visible = visible
                yield _sse({"type": "delta", "text": new})
    except Exception:
        failed = True

    if failed and not raw:
        # The model call failed before any text — mirror /api/chat.
        if mode == "normal":
            payload = {"reply": "I'm having trouble connecting right now. Please try again in a moment — I'm still here.", "crisis": False, "hotline": ctx["hotline"], "banner": ctx["banner"], "banner_lang": ctx["banner_lang"]}
            yield _sse({"type": "delta", "text": payload["reply"]})
            yield _sse({"type": "done", **payload})
            return
        raw = ctx["fallback_text"]

    if skip is None:
        skip = len("[CRISIS]") if raw.startswith("[CRISIS]") else 0
    payload = _finish_model_turn(ctx, mode, raw, background)
    final = payload["reply"]
    visible = scrub_prefix(raw[skip:].lstrip())
    if final.startswith(visible) and len(final) > len(visible):
        yield _sse({"type": "delta", "text": final[len(visible):]})
    yield _sse({"type": "done", **payload})


async def _chat_stream_events_fast(uid, msg, background):
    """Async front door for /api/chat/stream. A deterministic
    final turn (keyword crisis, completed overdose) is yielded
    immediately, in the event loop — no threadpool slot, no
    model call ahead of it. Model turns delegate to the sync
    generator, pulled chunk by chunk through the threadpool so
    one user's slow stream never blocks the loop — or another
    user's crisis reply."""
    uid = (uid or "").strip()
    msg = (msg or "").strip()
    if uid and msg:
        fast = _fast_final_turn(uid, msg)
        if fast is not None:
            yield _sse({"type": "delta", "text": fast["reply"]})
            yield _sse({"type": "done", **fast})
            return
    gen = _chat_stream_events(uid, msg, background)
    while True:
        chunk = await run_in_threadpool(next, gen, None)
        if chunk is None:
            break
        yield chunk


@app.post("/api/chat/stream")
async def chat_stream(body: ChatIn, background: BackgroundTasks):
    resp = StreamingResponse(
        _chat_stream_events_fast(
            body.user_id, body.message, background),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=background,
    )
    if body.user_id.strip():
        _set_uid_cookie(resp, body.user_id.strip())
    return resp


GREETING_SYSTEM = """You are Solace, a warm AI self-help companion. The person is returning after a break of a few hours or more. Write ONE short welcome-back line — a single sentence — that picks up where you left off, using the memory note and recent messages provided.

Rules:
- Warm and brief. Reference ONE thing they were working through, by its content.
- Today is {today}. Respect dates: if the thing you reference was a plan or event dated today or later, it has NOT happened — do not ask how it went; you can wish them well for it, or ask if they feel ready.
- Never claim you were thinking about them, missed them, or did anything while they were away — you are simply picking up the thread.
- If their recent messages are in Hebrew, write the line fully in Hebrew.
- Output only the line itself — no quotes, no preamble."""


class GreetingIn(BaseModel):
    user_id: str


@app.post("/api/greeting")
def greeting(body: GreetingIn):
    """Continuity opener for a returning visit: when the person has
    been away for hours, Solace speaks first with ONE line about
    last time. Server-checked: never while crisis mode is latched,
    and never twice for the same return — the saved opener becomes
    the newest message, so the age check closes behind it. The
    3-hour gap is measured from the person's latest activity of ANY
    kind — chat messages and rehearsal turns/debriefs alike: a
    rehearsal that ended a minute ago is not a 'return', and the
    opener must stay silent (Tab's 1-minute bug)."""
    uid = (body.user_id or "").strip()
    if not uid:
        return {"greeting": None}
    with db_lock:
        conn = _conn()
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u or u["crisis_mode"]:
            conn.close()
            return {"greeting": None}
        rows = conn.execute(
            "SELECT role, content, created_at FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 8",
            (uid,),
        ).fetchall()
        srow = conn.execute(
            "SELECT summary FROM summaries WHERE user_id=?", (uid,)
        ).fetchone()
        last_row = conn.execute(
            "SELECT MAX(ts) AS last_ts FROM ("
            "SELECT created_at AS ts FROM messages WHERE user_id=? "
            "UNION ALL "
            "SELECT updated_at AS ts FROM rehearsals WHERE user_id=?)",
            (uid, uid),
        ).fetchone()
        conn.close()
    summary = srow["summary"] if srow else ""
    if not rows and not summary:
        return {"greeting": None}
    last_ts = last_row["last_ts"] if last_row else None
    if not last_ts:
        return {"greeting": None}
    try:
        last_at = datetime.strptime(last_ts, "%Y-%m-%d %H:%M:%S")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if (now - last_at).total_seconds() < 3 * 3600:
            return {"greeting": None}
    except Exception:
        return {"greeting": None}
    convo = "\n".join(
        "%s: %s" % (r["role"], r["content"]) for r in reversed(rows))
    ask = "Memory note:\n%s\n\nRecent messages:\n%s" % (
        summary or "(none)", convo)
    try:
        line = call_anthropic(
            SUMMARY_MODEL,
            GREETING_SYSTEM.format(today=_today_str()),
            [{"role": "user", "content": ask}],
            max_tokens=120,
        )
    except Exception:
        return {"greeting": None}
    line = " ".join(line.strip().strip('"').split())[:300]
    line = scrub_field(line)
    if not line:
        return {"greeting": None}
    _save_assistant_msg(uid, line)
    return {"greeting": line}


# ------------------------------------------------------- Rehearse ----
# "Rehearse a hard talk": the user practices a difficult conversation
# before it happens. The model plays the other person, built from a
# quick setup (who, what the talk is about, how they usually react);
# afterwards a coach debriefs with concrete lines to say instead.
# Safety: every piece of user text in this mode — setup fields and
# every rehearsal turn — goes through the SAME pre-model risk check
# as normal chat (assess_risk: keyword layer, then classifier). A hit
# exits the mode and returns the frozen crisis response through the
# exact same latch/banner path as chat, byte-identical.

PERSON_TYPES = ("boss", "partner", "dad", "mom", "friend", "custom")

PERSONA_SYSTEM = """You are playing a real person in a PRACTICE conversation inside Solace, a self-help companion app. The user is rehearsing a difficult conversation they need to have in real life, and you are the other person in that conversation, so they can practice saying it out loud first. Afterwards a separate coach debriefs the user — giving feedback is NOT your job.

WHO YOU ARE
{who}
{user_line}The user has come to talk to you about: {topic}
{react_line}{context_line}

HOW TO PLAY
- Stay completely in character as this person for the whole rehearsal. Talk the way they would: their priorities, their tone, what they care about, what would worry or rub them the wrong way.
- Be realistic, not a pushover. If the user is vague, unfair, or springs something on you, push back the way this person genuinely would. If the user is clear, fair and honest, let it land — soften the way a real person would.
- Keep reactions human-sized: firm, defensive, hurt, stubborn, quiet, skeptical — yes. Shouting tirades, threats, cruelty or abuse — never.
- Short, natural turns — usually 1 to 3 sentences. Talk like a person in a real conversation, not a speech.
- Speak ONLY the words you say out loud. Never narrate your actions or body language, and never put an action in (parentheses). If a brief physical reaction truly matters, mark it with single asterisks — *leans back* — so it shows as a stage direction, not as part of your sentence. Keep such marks rare (at most one per reply) and short.
- Never break character: no advice, no feedback, no stepping out of the scene. If the user asks a meta question (about the app, whether you are real, anything outside the scene), deflect briefly in character — puzzled, or brushing it off the way this person would — and steer back to the conversation.
- Never bring up suicide or harming anyone — yours or the user's — as part of the scene, and never threaten anyone.
- Never use clinical or diagnostic labels about the user or yourself.
- Match the user's language: if they write in Hebrew, reply fully in Hebrew."""

DEBRIEF_SYSTEM = """You are the Solace coach, debriefing a PRACTICE conversation the user just finished. The user rehearsed a hard talk with {label}; an AI played that person. Your job is a short, concrete, kind debrief that makes the real conversation go better.

Rules:
- Communication coaching only. Never use clinical or diagnostic labels for the user or the other person, and never comment on anyone's mental health.
- Be specific: everything must tie to lines the user actually said in the transcript.
- Refer to the other person as {label}.
- Output ONLY a JSON object — no prose before or after — with exactly these keys:
  "worked": array of 2-3 short strings: specific things the user did that genuinely worked.
  "sideways_index": integer — the index (from the numbered list of the user's own lines given to you) of the line where the talk got hardest or went sideways.
  "sideways_note": 1-2 sentences, kind and plain: what made that moment hard.
  "say_instead": array of 2-3 exact alternative lines the user could say at that moment — real wording they can actually use, short and quotable, in the user's own voice.
  "outcome": one of "full", "partial", "none" — judged ONLY by what the other person actually agreed to in the transcript: "full" = they clearly agreed to what the user asked for; "partial" = they gave something real but short of the ask (a smaller step, or a concrete next move); "none" = they agreed to NOTHING — they refused, deflected, or the talk ended with no concession of any kind.
  "readiness": one honest closing line that MUST match "outcome":
    - "full": you may say they are ready for the real talk.
    - "partial": name the ground they won and the one thing still left to get.
    - "none": NEVER say the user is ready or prepared, and never imply it went well — they won no concession. Close with what they DID hold on to (they stayed calm, they said the hard sentence out loud) plus ONE concrete target for next time, in this shape: "<what they held>; next time get one concrete yes."
- DELIVERY (only when the request includes a "DELIVERY (measured)" block): add one more key, "delivery": an array of 2-3 short strings about HOW the user said it — pace, pauses, filler words, steadiness — using ONLY the measured facts in that block. Never invent a number or a quote; where the block gives an example line, quote it exactly. If the block reports long pauses (over 2 seconds), you MUST mention them, with the measured count, and say where each pause fell — the word it came after — exactly as the block states. Call a pause a pause "before you started" ONLY when the block explicitly reports a before-starting pause over ~2 seconds; never invent a pause location or a start gap the block does not report. If there is no DELIVERY block in the request, do not include a "delivery" key at all.
- Match the user's language: if the conversation is in Hebrew, every string in the JSON is in Hebrew."""


def _persona_who(rec):
    ptype = rec["person_type"]
    name = (rec["person_name"] or "").strip()
    rel = (rec["relationship"] or "").strip()
    if ptype == "boss":
        return "You are the user's boss at work."
    if ptype == "partner":
        return "You are the user's partner — the person they are in a relationship with."
    if ptype == "dad":
        return "You are the user's father."
    if ptype == "mom":
        return "You are the user's mother."
    if ptype == "friend":
        return "You are a close friend of the user."
    return "You are %s, the user's %s." % (
        name or "someone important", rel or "someone in their life")


def _rehearsal_person_label(rec):
    """Short third-person label for memory lines and history cards,
    e.g. 'their boss', 'their dad', or a custom name/relationship."""
    ptype = rec["person_type"]
    name = (rec["person_name"] or "").strip()
    rel = (rec["relationship"] or "").strip()
    if ptype == "boss":
        return "their boss"
    if ptype == "partner":
        return "their partner"
    if ptype == "dad":
        return "their dad"
    if ptype == "mom":
        return "their mom"
    if ptype == "friend":
        return "a friend"
    if name and rel:
        return "%s (their %s)" % (name, rel)
    if name:
        return name
    if rel:
        return "their %s" % rel
    return "someone"


def _persona_display(rec):
    """How the UI refers to the persona: 'Dad', 'Your boss', a name."""
    ptype = rec["person_type"]
    name = (rec["person_name"] or "").strip()
    known = {
        "boss": "Your boss", "partner": "Your partner", "dad": "Dad",
        "mom": "Mom", "friend": "Your friend",
    }
    return known.get(ptype) or name or (rec["relationship"] or "").strip() or "Them"


def _persona_system(rec, user_name):
    reactions = []
    try:
        reactions = [str(x) for x in json.loads(rec["reactions"] or "[]")]
    except Exception:
        reactions = []
    if reactions:
        react_line = (
            "How you usually react in a hard talk: %s. Let it show "
            "realistically when the moment calls for it — not in every line.\n"
            % "; ".join(reactions)
        )
    else:
        react_line = "React the way a real person in your position naturally would.\n"
    context_line = ""
    if (rec["context"] or "").strip():
        context_line = "Background the user shared: %s\n" % rec["context"].strip()
    user_line = ""
    if user_name:
        user_line = "The user's name is %s.\n" % user_name
    topic = (rec["topic"] or "").strip() or "something important they need to bring up"
    return PERSONA_SYSTEM.format(
        who=_persona_who(rec), user_line=user_line, topic=topic,
        react_line=react_line, context_line=context_line,
    )


def _persona_opener_direction(rec):
    topic = (rec["topic"] or "").strip() or "something important they need to bring up"
    return (
        "The practice conversation is starting now. The user has come "
        "to you to talk about: %s. Open with ONE short, natural line, "
        "in character — the way this conversation would really begin. "
        "Do not explain anything; just be the person, first line of "
        "the scene." % topic
    )


# Stage directions must never read as spoken dialogue (Tab fix).
# The persona is told to mark a rare brief action with *asterisks*,
# which the client renders italic; but models also leak them in
# (parentheses), where they show as plain text inside the spoken
# line. The cleaner below converts a parenthetical/bracketed span
# that is shaped like a stage direction into the asterisk form, and
# the stream holdback keeps a not-yet-closed span from flashing raw.

_STAGE_STARTERS = {
    "a", "the", "beat", "pause", "silence", "long", "deep", "he", "she",
    "leans", "leaning", "sighs", "sighing", "pauses", "looks", "looking",
    "smiles", "smiling", "laughs", "laughing", "nods", "nodding",
    "shrugs", "crosses", "folds", "stands", "sits", "shakes", "frowns",
    "clears", "takes", "rubs", "taps", "glances", "raises", "lowers",
    "turns", "waits", "breathes", "exhales", "inhales", "blinks",
    "stares", "grins", "winces", "scoffs", "chuckles", "shifts",
    "straightens", "softens", "stiffens", "hesitates", "swallows",
    "tilts", "narrows", "opens", "closes", "points", "waves", "pushes",
    "pulls", "puts", "sets", "picks", "pours", "sips", "checks",
    "quietly", "slowly", "softly",
}

_SPAN_RE = re.compile(r"[\(\[]([^\(\)\[\]\n]{1,80})[\)\]]")


def _stage_directionish(inner):
    """Is this short bracketed span a stage direction ('leans back
    slightly') rather than part of the spoken sentence?"""
    s = inner.strip().strip("*").strip()
    if not s or "*" in s or '"' in s or ":" in s:
        return False
    words = re.findall(r"[A-Za-z']+", s)
    if not (1 <= len(words) <= 8):
        return False
    # Stage directions are fragments — no sentence punctuation inside,
    # and they don't speak in first/second person.
    if re.search(r"[.!?]", s):
        return False
    if re.search(r"\b(I|you|we|my|your|me|am|are)\b", s):
        return False
    first = words[0].lower()
    if first in _STAGE_STARTERS:
        return True
    # "<verb>s ..." third-person fragment, e.g. 'drums fingers on desk'
    return bool(re.match(r"^[a-z]+s$", first)) and len(words) >= 2


def _clean_persona_text(text):
    """Convert leaked (parenthetical) stage directions to the
    *asterisk* form the client italicizes; drop a lone stray closing
    asterisk marker. Spoken text is otherwise untouched."""
    def repl(m):
        if _stage_directionish(m.group(1)):
            return "*" + m.group(1).strip() + "*"
        return m.group(0)
    out = _SPAN_RE.sub(repl, text)
    if out.count("*") % 2 == 1:
        i = out.rfind("*")
        out = out[:i] + out[i + 1:]
    return out


def _persona_safe_prefix(raw):
    """The longest prefix of a still-streaming reply that is safe to
    clean and emit: hold back a trailing unclosed ( or [ span (it may
    still close as a stage direction) and a trailing unclosed *
    span."""
    cut = len(raw)
    depth_at = None
    for i, ch in enumerate(raw):
        if ch in "([": 
            if depth_at is None:
                depth_at = i
        elif ch in ")]":
            depth_at = None
    if depth_at is not None:
        tail = raw[depth_at:]
        if "\n" not in tail and len(tail) <= 90:
            cut = depth_at
    prefix = raw[:cut]
    if prefix.count("*") % 2 == 1:
        tail = prefix[prefix.rfind("*"):]
        if "\n" not in tail and len(tail) <= 50:
            prefix = prefix[:prefix.rfind("*")]
    return prefix


def _load_transcript(rec):
    try:
        t = json.loads(rec["transcript"] or "[]")
        return t if isinstance(t, list) else []
    except Exception:
        return []


def _save_transcript(rid, transcript, state=None):
    with db_lock:
        c = _conn()
        if state:
            c.execute(
                "UPDATE rehearsals SET transcript=?, state=?, updated_at=datetime('now') WHERE id=?",
                (json.dumps(transcript), state, rid),
            )
        else:
            c.execute(
                "UPDATE rehearsals SET transcript=?, updated_at=datetime('now') WHERE id=?",
                (json.dumps(transcript), rid),
            )
        c.commit()
        c.close()


def _get_rehearsal(uid, rid):
    with db_lock:
        c = _conn()
        row = c.execute(
            "SELECT * FROM rehearsals WHERE id=? AND user_id=?", (rid, uid)
        ).fetchone()
        c.close()
    return dict(row) if row else None


def _persona_model_messages(rec, new_user_msg=None):
    """Model messages for a rehearsal turn: a constant stage-direction
    as the first user message, then the transcript (persona lines as
    assistant), then optionally the user's new line."""
    msgs = [{"role": "user", "content": _persona_opener_direction(rec)}]
    for t in _load_transcript(rec):
        if t.get("role") == "persona":
            msgs.append({"role": "assistant", "content": t.get("content", "")})
        else:
            msgs.append({"role": "user", "content": t.get("content", "")})
    if new_user_msg is not None:
        msgs.append({"role": "user", "content": new_user_msg})
    return msgs


def _rehearsal_hebrew(rec, msg):
    """Crisis-exit language for a rehearsal line: the language of
    THIS line only, never the rehearsal's or chat's history — the
    same per-message rule as normal chat (Tab round 3)."""
    return message_is_hebrew(msg)


def _rehearsal_crisis_exit(rec, saved_user_text, hebrew):
    """A crisis signal inside Rehearse mode: exit the mode, latch
    crisis state, and return the frozen response — the exact same
    payload shape and saved-message path as a normal-chat crisis."""
    uid = rec["user_id"]
    banner_lang = "he" if hebrew else "en"
    _set_user_state(uid, crisis_mode=True, stepdown_v=False,
                    banner_v=True, lang_v=banner_lang)
    with db_lock:
        c = _conn()
        if saved_user_text:
            c.execute(
                "INSERT INTO messages (user_id, role, content) VALUES (?,?,?)",
                (uid, "user", saved_user_text),
            )
        if rec.get("id"):
            c.execute(
                "UPDATE rehearsals SET state='ended', updated_at=datetime('now') WHERE id=?",
                (rec["id"],),
            )
        c.commit()
        c.close()
    if detect_overdose(saved_user_text or ""):
        # Medical emergency first here too (Tab ruling) — 911 /
        # מד״א 101 leads, not the suicide question.
        base = (EMERGENCY_RESPONSE_HE if hebrew
                else EMERGENCY_RESPONSE)
    else:
        base = (FIXED_CRISIS_RESPONSE_HE if hebrew
                else FIXED_CRISIS_RESPONSE)
    reply = base + crisis_resource_line(hebrew, base)
    _save_assistant_msg(uid, reply)
    return {"reply": reply, "crisis": True,
            "hotline": "1201" if hebrew else "988", "banner": True,
            "banner_lang": banner_lang, "exited": True,
            # The triggering user text (already saved above as a main
            # chat message) — echoed so the client can render it in
            # the main chat directly above this reply; without it the
            # person lands in chat seeing a reply with no context.
            "user_message": saved_user_text or ""}


class RehearseStartIn(BaseModel):
    user_id: str
    person_type: str = "custom"
    person_name: str = ""
    relationship: str = ""
    topic: str = ""
    reactions: list = []
    context: str = ""


class RehearseTurnIn(BaseModel):
    user_id: str
    rehearsal_id: int
    message: "str | None" = None  # None => the persona's opening line


class RehearseDebriefIn(BaseModel):
    user_id: str
    rehearsal_id: int
    # Voice Rehearse: client-measured spoken turns ({text, speak_ms,
    # pause_before_ms, longest_pause_ms}). Solace receives only the
    # transcript text and timings — never audio. (The phone's own
    # speech-recognition service may process the audio separately.)
    # Empty for typed-only rehearsals.
    voice_turns: list = []


@app.post("/api/rehearse/start")
def rehearse_start(body: RehearseStartIn):
    uid = (body.user_id or "").strip()
    if not uid:
        return {"ok": False, "error": "no_user"}
    ptype = body.person_type if body.person_type in PERSON_TYPES else "custom"
    name = (body.person_name or "").strip()[:80]
    rel = (body.relationship or "").strip()[:80]
    topic = (body.topic or "").strip()[:1000]
    context = (body.context or "").strip()[:1000]
    reactions = [
        str(x).strip()[:60] for x in (body.reactions or []) if str(x).strip()
    ][:6]
    with db_lock:
        conn = _conn()
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            conn.execute("INSERT INTO users (id) VALUES (?)", (uid,))
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        profile = dict(row)
        conn.close()
    # While crisis mode is latched, Solace stays in support mode —
    # rehearsal waits until things have stepped back down.
    if profile.get("crisis_mode"):
        return {"ok": False, "reason": "crisis_mode", "crisis": True,
                "banner": True,
                "banner_lang": profile.get("crisis_lang") or "en"}
    # The setup text is user text: same pre-model risk check as chat.
    setup_text = "\n".join(x for x in (topic, context, rel, name) if x)
    rec_stub = {"id": None, "user_id": uid, "transcript": "[]"}
    if setup_text and assess_risk(setup_text):
        payload = _rehearsal_crisis_exit(
            rec_stub, topic or context, has_hebrew(setup_text))
        return {"ok": False, **payload}
    with db_lock:
        conn = _conn()
        cur = conn.execute(
            "INSERT INTO rehearsals (user_id, person_type, person_name, "
            "relationship, topic, reactions, context, state) "
            "VALUES (?,?,?,?,?,?,?, 'rehearsing')",
            (uid, ptype, name, rel, topic, json.dumps(reactions), context),
        )
        rid = cur.lastrowid
        conn.commit()
        conn.close()
    stats_feature(uid, "rehearse")
    return {"ok": True, "rehearsal_id": rid,
            "person_display": _persona_display(
                {"person_type": ptype, "person_name": name,
                 "relationship": rel})}


def _rehearse_stream_events(uid, rid, message):
    rec = _get_rehearsal(uid, rid)
    if not rec:
        yield _sse({"type": "error", "error": "not_found"})
        return
    if rec["state"] != "rehearsing":
        yield _sse({"type": "error", "error": "not_active"})
        return
    transcript = _load_transcript(rec)
    opener = message is None
    if opener and transcript:
        yield _sse({"type": "error", "error": "already_started"})
        return
    if not opener:
        message = (message or "").strip()
        if not message:
            yield _sse({"type": "error", "error": "empty"})
            return
        # Pre-model risk check on the user's line — identical to chat.
        # A hit exits the mode with the frozen response.
        if assess_risk(message):
            payload = _rehearsal_crisis_exit(
                rec, message, _rehearsal_hebrew(rec, message))
            transcript.append({"role": "user", "content": message})
            _save_transcript(rid, transcript, state="ended")
            # NO delta for a crisis exit (Tab ruling): the client
            # pumps streamed deltas into speech as they arrive, and
            # a crisis line must NEVER play as audio. The done
            # payload carries the full reply and the client renders
            # it as text via rhCrisisExit.
            yield _sse({"type": "done", **payload})
            return
        transcript.append({"role": "user", "content": message})
        _save_transcript(rid, transcript)

    with db_lock:
        c = _conn()
        u = c.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
        c.close()
    system = _persona_system(rec, (u["name"] if u else "") or "")
    msgs = _persona_model_messages(rec, None if opener else message)

    raw = ""
    emitted = ""
    failed = False
    try:
        for chunk in _anthropic_stream_chunks(system, msgs):
            raw += chunk
            # Emit only the cleaned safe prefix, so a stage
            # direction never flashes as plain dialogue mid-stream —
            # and run it through the output filter's streaming form,
            # so a banned word never flashes either.
            safe = scrub_prefix(
                _clean_persona_text(_persona_safe_prefix(raw)))
            if safe.startswith(emitted) and len(safe) > len(emitted):
                yield _sse({"type": "delta", "text": safe[len(emitted):]})
                emitted = safe
    except Exception:
        failed = True
    if failed and not raw:
        # Roll back the just-saved user line so a retry doesn't
        # duplicate the turn, and let the client offer a retry.
        if not opener:
            transcript = transcript[:-1]
            _save_transcript(rid, transcript)
        yield _sse({"type": "error", "error": "connection"})
        return
    reply = scrub_reply(_clean_persona_text(raw).strip())
    if reply.startswith(emitted) and len(reply) > len(emitted):
        yield _sse({"type": "delta", "text": reply[len(emitted):]})
    transcript.append({"role": "persona", "content": reply})
    _save_transcript(rid, transcript)
    yield _sse({"type": "done", "reply": reply, "crisis": False,
                "mode": "rehearse"})


@app.post("/api/rehearse/stream")
def rehearse_stream(body: RehearseTurnIn):
    uid = (body.user_id or "").strip()
    resp = StreamingResponse(
        _rehearse_stream_events(uid, body.rehearsal_id, body.message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    if uid:
        _set_uid_cookie(resp, uid)
    return resp


def _fallback_debrief(rec, user_lines):
    quote = max(user_lines, key=len) if user_lines else ""
    idx = user_lines.index(quote) if quote else 0
    return {
        "worked": ["You showed up and said it out loud — practicing "
                   "the words once makes the real talk feel familiar."],
        "sideways_index": idx,
        "sideways_quote": quote,
        "sideways_note": "This was the hardest moment in the practice.",
        "say_instead": [
            "Try opening with the point, plainly: \u201cThere's "
            "something important I need to talk about.\u201d",
            "Try naming what you need: \u201cWhat I need is\u2026\u201d",
        ],
        "readiness": "You practiced it once — the real talk will "
                     "feel more familiar than you expect.",
        "outcome": "",
    }


def _scrub_debrief(debrief):
    """Run every model-written debrief field through the output
    filter (Tab ruling): worked / say-instead / delivery items are
    scrubbed sentence-by-sentence and dropped if they empty out;
    sideways_note and readiness fall back to neutral lines. The
    sideways QUOTE is the user's own exact words — never filtered."""
    if not isinstance(debrief, dict):
        return debrief
    out = dict(debrief)
    for key in ("worked", "say_instead", "delivery"):
        if key not in out:
            continue  # never invent a section the debrief didn't have
        items = []
        for x in out.get(key) or []:
            s = scrub_field(str(x))
            if s:
                items.append(s)
        out[key] = items
    if not out.get("worked"):
        out["worked"] = ["You showed up and practiced — that counts."]
    note = scrub_field(str(out.get("sideways_note", "")))
    out["sideways_note"] = note or "That moment is worth another look."
    ready = scrub_field(str(out.get("readiness", "")))
    out["readiness"] = ready or "You practiced it — you're readier than you were."
    return out


# ------------------------------------------------ Voice Rehearse ----
# Delivery coaching (Tab spec): HOW the user said it, measured in
# code from the spoken turns — never invented by the model. The
# browser's speech recognition produces each spoken line's transcript
# plus timings (the speech/capture interval from recognition or
# speech start through speech end — not just first-to-last result —
# pause before it, longest mid-line pause, and each mid-line pause
# event with the last word recognized before it began); the client
# sends those
# with the debrief request and the
# numbers below are computed here. Typed-only rehearsals send no
# voice turns and get no delivery section — no fake data.

_FILLER_PHRASES_EN = ("you know", "i mean")
_FILLER_RE_EN = re.compile(r"\b(?:um+|uh+|erm?)\b", re.I)
_LIKE_PREV_EXCLUDE = {
    "would", "could", "feel", "feels", "felt", "look", "looks",
    "looked", "seem", "seems", "seemed", "is", "was", "be", "been",
    "it", "that", "this", "don't", "didn't", "i'd", "you'd",
}
_FILLER_HE = ("אממ", "אהה", "כאילו", "יעני")
_HEDGE_RES_EN = [
    re.compile(p, re.I) for p in (
        r"i'?m sorry", r"\bsorry\b", r"\bjust\b", r"\bmaybe\b",
        r"\bi guess\b", r"\bkind of\b", r"\bsort of\b", r"\bi think\b",
        r"\bif that'?s (okay|ok)\b", r"\bi don'?t know\b",
        r"\bwhatever you think\b", r"\bno worries if\b", r"\bprobably\b",
    )
]
_HEDGE_HE = ("סליחה", "אולי", "נראה לי", "לא יודע", "לא יודעת",
             "אם זה בסדר", "כזה")


def _count_fillers(text):
    """Filler-word count + per-word detail for one line of speech."""
    low = " %s " % (text or "").lower()
    total = 0
    detail = {}
    for m in _FILLER_RE_EN.finditer(low):
        total += 1
        detail["um/uh"] = detail.get("um/uh", 0) + 1
    for ph in _FILLER_PHRASES_EN:
        # Word boundaries, so "you know," (comma attached) still
        # counts — a padded-space search missed exactly that shape.
        n = len(re.findall(r"\b%s\b" % re.escape(ph), low))
        if n:
            total += n
            detail[ph] = detail.get(ph, 0) + n
    # "like" as a filler: a standalone token that is not the verb /
    # comparison sense (guarded by the previous word).
    toks = re.findall(r"[a-z']+", (text or "").lower())
    like_n = 0
    for i, tok in enumerate(toks):
        if tok == "like" and (i == 0 or toks[i - 1] not in _LIKE_PREV_EXCLUDE):
            like_n += 1
    if like_n:
        total += like_n
        detail["like"] = like_n
    for f in _FILLER_HE:
        n = (text or "").count(f)
        if n:
            total += n
            detail[f] = detail.get(f, 0) + n
    return total, detail


def _count_hedges(text):
    n = 0
    for rx in _HEDGE_RES_EN:
        n += len(rx.findall(text or ""))
    for h in _HEDGE_HE:
        n += (text or "").count(h)
    return n


def _norm_line(text):
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _delivery_facts(voice_turns, user_lines):
    """Measured delivery facts for the spoken turns of a rehearsal,
    or None when nothing usable was spoken. voice_turns entries are
    client-measured: {text, speak_ms, pause_before_ms,
    longest_pause_ms, pauses}; "pauses" is the client's per-line
    list of mid-line pause events {ms, after} — "after" is the last
    word recognized before that pause began — so coaching can say
    WHERE a pause fell ("You paused 2.5s after 'workload'"). Each
    turn is matched to its transcript line by exact (normalized)
    text, in order, so quotes stay the user's exact words from the
    transcript itself."""
    if not voice_turns or not user_lines:
        return None
    remaining = list(range(len(user_lines)))
    matched = []
    for vt in voice_turns[:40]:
        if not isinstance(vt, dict):
            continue
        text = str(vt.get("text") or "").strip()
        if not text:
            continue

        def _ms(key):
            try:
                return max(0, min(int(vt.get(key) or 0), 600000))
            except (TypeError, ValueError):
                return 0

        hit = None
        for i in remaining:
            if _norm_line(user_lines[i]) == _norm_line(text):
                hit = i
                break
        if hit is None:
            continue
        remaining.remove(hit)
        # Located mid-line pauses for this line: only events at or
        # over the ~2 s coaching threshold, with a usable location
        # word, sanitized (bounded count/length).
        located = []
        raw_pauses = vt.get("pauses")
        if isinstance(raw_pauses, list):
            for ev in raw_pauses[:6]:
                if not isinstance(ev, dict):
                    continue
                try:
                    ms = max(0, min(int(ev.get("ms") or 0), 600000))
                except (TypeError, ValueError):
                    continue
                after = str(ev.get("after") or "").strip()[:60]
                if ms >= 2000 and after:
                    located.append({"ms": ms, "after": after})
        matched.append({
            "line": user_lines[hit],
            "speak_ms": _ms("speak_ms"),
            "pause_before_ms": _ms("pause_before_ms"),
            "longest_pause_ms": _ms("longest_pause_ms"),
            "pauses": located,
        })
    if not matched:
        return None
    words = 0
    speak_ms = 0
    for m in matched:
        w = len(re.findall(r"\S+", m["line"]))
        m["words"] = w
        if m["speak_ms"] >= 400:
            words += w
            speak_ms += m["speak_ms"]
    wpm = None
    band = None
    if speak_ms > 0 and words > 0:
        wpm = int(round(words / (speak_ms / 60000.0)))
        band = "slow" if wpm < 110 else ("fast" if wpm > 170 else "natural")
    long_before = [m for m in matched if m["pause_before_ms"] >= 3000]
    mid_pauses = [m for m in matched if m["longest_pause_ms"] >= 2000]
    # Combined over-~2s pauses (before a line or mid-line): the
    # coaching must name these explicitly when they happened.
    long_pause_events = 0
    longest_pause_over_2_s = 0.0
    long_pause_examples = []
    for m in matched:
        for key in ("pause_before_ms", "longest_pause_ms"):
            if m[key] >= 2000:
                long_pause_events += 1
                longest_pause_over_2_s = max(
                    longest_pause_over_2_s, m[key] / 1000.0)
                if m["line"] not in long_pause_examples:
                    long_pause_examples.append(m["line"])
    # Located pauses (Tab fix): each coached mid-line pause carries
    # the word it followed, so coaching can name where it fell.
    pause_locations = []
    for m in matched:
        for ev in m["pauses"]:
            pause_locations.append({
                "s": round(ev["ms"] / 1000.0, 1),
                "after": ev["after"],
                "line": m["line"],
            })
    pause_locations = pause_locations[:4]
    # The start gap (silence before a line began) is a reportable
    # fact ONLY when it is over ~2 seconds; at or under that the
    # coaching must not mention a start gap at all.
    start_gaps = [m for m in matched if m["pause_before_ms"] > 2000]
    fillers_total = 0
    filler_detail = {}
    filler_line = ""
    filler_line_n = 0
    hedge_total = 0
    hedge_lines = []
    for m in matched:
        n, det = _count_fillers(m["line"])
        fillers_total += n
        for k, v in det.items():
            filler_detail[k] = filler_detail.get(k, 0) + v
        if n > filler_line_n:
            filler_line_n = n
            filler_line = m["line"]
        h = _count_hedges(m["line"])
        hedge_total += h
        if h and len(hedge_lines) < 2:
            hedge_lines.append(m["line"])
    detail_top = sorted(filler_detail.items(), key=lambda kv: -kv[1])[:3]
    return {
        "turns": len(matched),
        "words": words,
        "wpm": wpm,
        "pace_band": band,
        "long_pauses_before": len(long_before),
        "longest_pause_before_s": (
            round(max(m["pause_before_ms"] for m in long_before) / 1000.0, 1)
            if long_before else 0),
        "mid_pauses": len(mid_pauses),
        "longest_mid_pause_s": (
            round(max(m["longest_pause_ms"] for m in mid_pauses) / 1000.0, 1)
            if mid_pauses else 0),
        "long_pause_events": long_pause_events,
        "longest_pause_over_2_s": round(longest_pause_over_2_s, 1),
        "long_pause_examples": long_pause_examples[:2],
        "pause_locations": pause_locations,
        "start_gaps_over_2s": len(start_gaps),
        "longest_start_gap_s": (
            round(max(m["pause_before_ms"] for m in start_gaps) / 1000.0, 1)
            if start_gaps else 0),
        "fillers_total": fillers_total,
        "fillers_detail": detail_top,
        "filler_line": filler_line if filler_line_n else "",
        "hedge_total": hedge_total,
        "hedge_lines": hedge_lines,
    }


def _delivery_facts_block(f):
    """The measured facts as plain sentences for the coach prompt."""
    lines = [
        "DELIVERY (measured in code from the user's spoken turns — "
        "facts, not impressions):",
        "- Spoken lines measured: %d." % f["turns"],
    ]
    if f["wpm"]:
        lines.append(
            "- Overall pace: about %d words per minute (%s)."
            % (f["wpm"], {"slow": "on the slower side",
                          "natural": "a natural, conversational pace",
                          "fast": "fast"}[f["pace_band"]]))
    if f.get("long_pause_events"):
        lines.append(
            "- Long pauses (over 2 seconds, before or during a "
            "line): %d; the longest was about %s seconds."
            % (f["long_pause_events"], f["longest_pause_over_2_s"]))
        for q in f.get("long_pause_examples", []):
            lines.append(
                '- A line with a long pause, exactly: "%s"' % q)
    for loc in f.get("pause_locations", []):
        lines.append(
            '- Pause location: a pause of about %s seconds came '
            'right after "%s".' % (loc["s"], loc["after"]))
    if f.get("start_gaps_over_2s"):
        lines.append(
            "- Before starting a line, the user paused over ~2 "
            "seconds %d time(s); the longest such pause was about "
            "%s seconds. This may be described as a pause \"before "
            "you started\". No other start gap is reported — do "
            "not mention one." % (f["start_gaps_over_2s"],
                                  f["longest_start_gap_s"]))
    if f["fillers_total"]:
        det = ", ".join("%s ×%d" % kv for kv in f["fillers_detail"])
        lines.append("- Filler words: %d total (%s)." % (f["fillers_total"], det))
        if f["filler_line"]:
            lines.append(
                '- The line where fillers clustered, exactly: "%s"'
                % f["filler_line"])
    else:
        lines.append("- Filler words: none counted.")
    if f["hedge_total"]:
        lines.append(
            "- Hedging / unsure phrasing (sorry, just, maybe, "
            "I guess…): %d times." % f["hedge_total"])
        for q in f["hedge_lines"]:
            lines.append('- A line with hedging, exactly: "%s"' % q)
    else:
        lines.append("- Hedging phrases: none counted — the wording "
                     "was direct.")
    return "\n".join(lines)


def _delivery_notes_code(f, hebrew):
    """Deterministic delivery notes built only from the measured
    facts — the fallback when the coach's reply has no usable
    delivery section, so the numbers the user sees are always the
    measured ones."""
    notes = []
    if hebrew:
        if f["wpm"]:
            band = {"slow": "בצד האיטי והמדוד",
                    "natural": "קצב טבעי של שיחה",
                    "fast": "מהר — הילוך קצת יותר איטי יכול לעזור לדברים להישמע"}[
                        f["pace_band"]]
            notes.append("הקצב שלך היה בערך %d מילים לדקה — %s."
                         % (f["wpm"], band))
        if f.get("pause_locations"):
            locs = f["pause_locations"][:2]
            if len(locs) == 1:
                where = "אחרי המילה \u201c%s\u201d" % locs[0]["after"]
                notes.append(
                    "עצרת בערך %s שניות %s. עצירה יכולה להישמע כמו "
                    "איסוף מחשבות, לא כמו חולשה."
                    % (locs[0]["s"], where))
            else:
                notes.append(
                    "עצרת בערך %s שניות אחרי המילה \u201c%s\u201d, ועוד "
                    "%s שניות אחרי המילה \u201c%s\u201d. עצירה יכולה "
                    "להישמע כמו איסוף מחשבות, לא כמו חולשה."
                    % (locs[0]["s"], locs[0]["after"],
                       locs[1]["s"], locs[1]["after"]))
        elif f.get("long_pause_events"):
            notes.append(
                "היו %d הפסקות של יותר מ־2 שניות (הארוכה בערך %s שניות). "
                "הפסקה יכולה להישמע כמו איסוף מחשבות, לא כמו חולשה."
                % (f["long_pause_events"], f["longest_pause_over_2_s"]))
        if f["fillers_total"]:
            notes.append(
                "מילות מילוי: %d בסך הכול. הן לא הורסות כלום — אבל משפט "
                "אחד נקי, בלי עצירות מילוי, נשמע הרבה יותר בטוח." % f["fillers_total"])
        if f["hedge_lines"]:
            notes.append(
                "במשפט \u201c%s\u201d היו ריכוכים (סליחה / אולי / נראה לי) — "
                "אותו משפט בלי הריכוך נשמע תקיף יותר." % f["hedge_lines"][0])
        if f["long_pauses_before"]:
            notes.append(
                "לפני %d מהמשפטים הייתה הפסקה של 3 שניות או יותר (הארוכה "
                "בערך %s שניות). הפסקה זה בסדר — היא נשמעת כמו מחשבה, לא "
                "כמו חולשה." % (f["long_pauses_before"], f["longest_pause_before_s"]))
        return notes[:3]
    if f["wpm"]:
        band = {"slow": "on the slower, measured side",
                "natural": "a natural, conversational pace",
                "fast": "fast — easing off a touch can help the ask land"}[
                    f["pace_band"]]
        notes.append("Your pace averaged about %d words per minute — %s."
                     % (f["wpm"], band))
    if f.get("pause_locations"):
        locs = f["pause_locations"][:2]
        if len(locs) == 1:
            notes.append("You paused about %s seconds after "
                         "\u201c%s.\u201d A pause can read as "
                         "collecting your thoughts, not weakness."
                         % (locs[0]["s"], locs[0]["after"]))
        else:
            notes.append("You paused about %s seconds after "
                         "\u201c%s\u201d, and about %s seconds after "
                         "\u201c%s.\u201d A pause can read as "
                         "collecting your thoughts, not weakness."
                         % (locs[0]["s"], locs[0]["after"],
                            locs[1]["s"], locs[1]["after"]))
    elif f.get("long_pause_events"):
        notes.append("There were %d pauses over 2 seconds (longest "
                     "about %s seconds). A pause can read as "
                     "collecting your thoughts, not weakness."
                     % (f["long_pause_events"],
                        f["longest_pause_over_2_s"]))
    if f["fillers_total"]:
        det = ", ".join("%s ×%d" % kv for kv in f["fillers_detail"])
        notes.append("Filler words: %d total (%s). One clean sentence "
                     "without them sounds noticeably firmer."
                     % (f["fillers_total"], det))
    if f["hedge_lines"]:
        notes.append("Hedging showed up in \u201c%s\u201d — the same line "
                     "without the softener makes the same ask sound "
                     "steadier." % f["hedge_lines"][0])
    if f["long_pauses_before"]:
        notes.append("Before %d of your lines there was a pause of 3 "
                     "seconds or more (longest about %s seconds). A "
                     "pause reads as thinking, not weakness."
                     % (f["long_pauses_before"], f["longest_pause_before_s"]))
    return notes[:3]


@app.post("/api/rehearse/debrief")
def rehearse_debrief(body: RehearseDebriefIn, background: BackgroundTasks):
    uid = (body.user_id or "").strip()
    rec = _get_rehearsal(uid, body.rehearsal_id)
    if not rec:
        return {"ok": False, "error": "not_found"}
    if rec["state"] == "debriefed" and rec["debrief"]:
        try:
            return {"ok": True,
                    "debrief": _scrub_debrief(json.loads(rec["debrief"]))}
        except Exception:
            pass
    if rec["state"] != "rehearsing":
        return {"ok": False, "error": "not_active"}
    transcript = _load_transcript(rec)
    user_lines = [t.get("content", "") for t in transcript
                  if t.get("role") == "user" and t.get("content", "").strip()]
    label = _rehearsal_person_label(rec)
    # Voice Rehearse: delivery facts measured in code from the
    # spoken turns (None for typed-only — no delivery section then).
    try:
        delivery_facts = _delivery_facts(body.voice_turns, user_lines)
    except Exception:
        delivery_facts = None
    if delivery_facts:
        # The visitor actually spoke lines in this rehearsal.
        stats_feature(body.user_id, "voice")
    if not user_lines:
        debrief = {
            "worked": ["You took the first step and set the talk up — "
                       "that counts."],
            "sideways_index": 0, "sideways_quote": "",
            "sideways_note": "The practice ended before the talk "
                             "really got going — that happens.",
            "say_instead": [],
            "readiness": "Next time, try staying for one more line "
                         "than feels comfortable.",
            "outcome": "",
        }
    else:
        script = "\n".join(
            "%s: %s" % (("You" if t.get("role") == "user" else "Them"),
                        t.get("content", ""))
            for t in transcript
        )
        numbered = "\n".join(
            "%d. %s" % (i, line) for i, line in enumerate(user_lines))
        ask = (
            "The talk was about: %s\n\nTranscript:\n%s\n\n"
            "The user's own lines, numbered:\n%s\n\n"
            "Return the JSON debrief now. sideways_index must be one "
            "of the numbers above." % (
                (rec["topic"] or "").strip() or "(not specified)",
                script, numbered)
        )
        if delivery_facts:
            ask += "\n\n" + _delivery_facts_block(delivery_facts)
        debrief = None
        try:
            out = call_anthropic(
                CHAT_MODEL,
                DEBRIEF_SYSTEM.format(label=label),
                [{"role": "user", "content": ask}],
                max_tokens=700,
            )
            a, b = out.find("{"), out.rfind("}")
            data = json.loads(out[a:b + 1])
            idx = int(data.get("sideways_index", 0))
            if not (0 <= idx < len(user_lines)):
                idx = 0
            worked = [str(x).strip() for x in data.get("worked", [])
                      if str(x).strip()][:3]
            instead = [str(x).strip() for x in data.get("say_instead", [])
                       if str(x).strip()][:3]
            if worked and len(instead) >= 2 and str(
                    data.get("sideways_note", "")).strip():
                outcome = str(data.get("outcome", "")).strip().lower()
                if outcome not in ("full", "partial", "none"):
                    outcome = ""
                readiness = str(data.get("readiness", "")).strip() \
                    or "You practiced it — you're readier than you were."
                if outcome == "none" and re.search(
                        r"ready|readier|מוכן|מוכנה", readiness, re.I):
                    # The rehearsal won NO concession — the closing
                    # must never declare the user ready (Tab's Dave
                    # case: he agreed to nothing, yet the debrief
                    # said "you're genuinely ready"). Held ground +
                    # one concrete next target instead.
                    readiness = (
                        "עמדת על שלך; בפעם הבאה נסה להשיג כן אחד ברור."
                        if any(has_hebrew(x) for x in user_lines) else
                        "You held your ground; next time get one "
                        "concrete yes.")
                debrief = {
                    "worked": worked,
                    "sideways_index": idx,
                    # The quote is taken from the transcript itself,
                    # so it is always the user's exact words.
                    "sideways_quote": user_lines[idx],
                    "sideways_note": str(data["sideways_note"]).strip(),
                    "say_instead": instead,
                    "readiness": readiness,
                    "outcome": outcome,
                }
                if delivery_facts:
                    notes = [str(x).strip() for x in
                             data.get("delivery", [])
                             if str(x).strip()][:3]
                    if not notes:
                        notes = _delivery_notes_code(
                            delivery_facts,
                            any(has_hebrew(x) for x in user_lines))
                    if notes:
                        debrief["delivery"] = notes
        except Exception:
            debrief = None
        if debrief is None:
            debrief = _fallback_debrief(rec, user_lines)
            if delivery_facts:
                notes = _delivery_notes_code(
                    delivery_facts,
                    any(has_hebrew(x) for x in user_lines))
                if notes:
                    debrief["delivery"] = notes
    debrief = _scrub_debrief(debrief)
    with db_lock:
        c = _conn()
        c.execute(
            "UPDATE rehearsals SET debrief=?, state='debriefed', "
            "updated_at=datetime('now') WHERE id=?",
            (json.dumps(debrief), rec["id"]),
        )
        c.commit()
        c.close()
    # A completed rehearsal contributes to "What Solace remembers"
    # through the existing memory updater (which now folds recent
    # debriefed rehearsals into the note and items).
    background.add_task(update_summary, uid)
    return {"ok": True, "debrief": debrief,
            "person_label": label, "person_display": _persona_display(rec)}


# ------------------------------------------ Real-talk follow-up ----
# After a debrief, the user may say when the REAL talk is. Only the
# minimal context is kept — who it is with (a display phrase) and
# when — never the topic or any transcript text. On the next visit
# after that time, Solace opens by asking how it went, once.

class RehearseFollowupIn(BaseModel):
    user_id: str
    rehearsal_id: int
    due_at: str
    person_display: str = ""
    lang: str = "en"


class FollowupDueIn(BaseModel):
    user_id: str


def _followup_person_display(rec, lang, requested=""):
    ptype = rec["person_type"]
    if lang == "he":
        known = {
            "boss": "הבוס שלך", "partner": "בן/בת הזוג שלך",
            "dad": "אבא", "mom": "אמא", "friend": "החבר/ה שלך",
        }
    else:
        known = {
            "boss": "your boss", "partner": "your partner",
            "dad": "Dad", "mom": "Mom", "friend": "your friend",
        }
    if ptype in known:
        return known[ptype]
    req = re.sub(r"\s+", " ", (requested or "")).strip()[:80]
    if req:
        return req
    name = (rec["person_name"] or "").strip()
    if name:
        return name
    rel = (rec["relationship"] or "").strip()
    if rel:
        return ("their %s" % rel) if lang != "he" else rel
    return "them" if lang != "he" else "הם"


def _parse_due_at(value):
    """Parse an ISO date/time into the DB's UTC-naive convention.
    Naive values are read as UTC (the client sends UTC ISO)."""
    raw = (value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@app.post("/api/rehearse/followup")
def rehearse_followup(body: RehearseFollowupIn):
    """Save when the real talk is, for one debriefed rehearsal.
    Skipping is a client-side no-call — nothing is stored, and no
    follow-up is ever fabricated without a person AND a time."""
    uid = (body.user_id or "").strip()
    rec = _get_rehearsal(uid, body.rehearsal_id) if uid else None
    if not rec or rec["state"] != "debriefed":
        return {"ok": False, "error": "not_debriefed"}
    try:
        due = _parse_due_at(body.due_at)
    except Exception:
        return {"ok": False, "error": "bad_time"}
    lang = "he" if (body.lang or "").lower().startswith("he") else "en"
    display = _followup_person_display(rec, lang, body.person_display)
    if not display:
        return {"ok": False, "error": "no_person"}
    with db_lock:
        conn = _conn()
        conn.execute(
            "INSERT INTO real_talk_followups (user_id, person_display,"
            " lang, due_at, asked) VALUES (?,?,?,?,0)"
            " ON CONFLICT(user_id) DO UPDATE SET"
            " person_display=excluded.person_display,"
            " lang=excluded.lang, due_at=excluded.due_at, asked=0,"
            " created_at=datetime('now')",
            (uid, display, lang, due.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        conn.close()
    return {"ok": True, "due_at": due.strftime("%Y-%m-%d %H:%M:%S"),
            "person_display": display}


@app.post("/api/rehearse/followup/due")
def rehearse_followup_due(body: FollowupDueIn):
    """The next-visit opener: if a saved real-talk time has passed,
    ask how it went — exactly once (marked asked as it is returned).
    Never while crisis mode is latched."""
    uid = (body.user_id or "").strip()
    if not uid:
        return {"greeting": None}
    with db_lock:
        conn = _conn()
        u = conn.execute(
            "SELECT crisis_mode FROM users WHERE id=?", (uid,)).fetchone()
        if not u or u["crisis_mode"]:
            conn.close()
            return {"greeting": None}
        row = conn.execute(
            "SELECT person_display, lang, due_at FROM"
            " real_talk_followups WHERE user_id=? AND asked=0",
            (uid,),
        ).fetchone()
        if not row:
            conn.close()
            return {"greeting": None}
        try:
            due = _parse_due_at(row["due_at"])
        except Exception:
            conn.close()
            return {"greeting": None}
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if due > now:
            conn.close()
            return {"greeting": None}
        cur = conn.execute(
            "UPDATE real_talk_followups SET asked=1"
            " WHERE user_id=? AND asked=0", (uid,))
        conn.commit()
        changed = cur.rowcount
        conn.close()
    if not changed:
        return {"greeting": None}
    person = row["person_display"]
    if row["lang"] == "he":
        line = "איך הייתה השיחה עם %s?" % person
    else:
        line = "How did it go with %s?" % person
    _save_assistant_msg(uid, line)
    return {"greeting": line, "person_display": person}


@app.post("/api/delete")
def delete_data(body: DeleteIn, response: Response):
    """Delete ALL data stored for a user_id (re-test fix #3): profile,
    every message, the memory/summary note, the memory items and
    tombstones, and all crisis state. Backs the in-app
    "Delete my data" button."""
    uid = body.user_id.strip()
    if not uid:
        return {"ok": False, "deleted": False}
    with db_lock:
        conn = _conn()
        conn.execute("DELETE FROM messages WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM summaries WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM memory_items WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM memory_tombstones WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM rehearsals WHERE user_id=?", (uid,))
        conn.execute(
            "DELETE FROM real_talk_followups WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
        conn.close()
    response.delete_cookie(UID_COOKIE, path="/")
    return {"ok": True, "deleted": True}


@app.get("/api/memory")
def get_memory(user_id: str, response: Response):
    """The "What Solace remembers about you" view data: the memory
    note plus the structured items (themes/goals — describing
    language only, never labels)."""
    if user_id.strip():
        _set_uid_cookie(response, user_id.strip())
    with db_lock:
        conn = _conn()
        s = conn.execute(
            "SELECT summary FROM summaries WHERE user_id=?", (user_id,)
        ).fetchone()
        rows = conn.execute(
            "SELECT id, kind, text FROM memory_items WHERE user_id=? ORDER BY id ASC",
            (user_id,),
        ).fetchall()
        conn.close()
    return {
        "summary": s["summary"] if s else "",
        "items": [dict(r) for r in rows],
    }


@app.post("/api/memory/delete-item")
def delete_memory_item(body: MemoryItemDeleteIn):
    """Remove ONE memory item from the remembers view. The item's text
    is tombstoned so a later memory refresh does not silently bring
    it back."""
    uid = body.user_id.strip()
    if not uid:
        return {"ok": False, "items": []}
    with db_lock:
        conn = _conn()
        row = conn.execute(
            "SELECT text FROM memory_items WHERE id=? AND user_id=?",
            (body.item_id, uid),
        ).fetchone()
        if row:
            conn.execute(
                "INSERT OR IGNORE INTO memory_tombstones (user_id, norm, created_at) "
                "VALUES (?,?,datetime('now'))",
                (uid, _norm_item(row["text"])),
            )
            conn.execute(
                "DELETE FROM memory_items WHERE id=? AND user_id=?",
                (body.item_id, uid),
            )
            conn.commit()
        rows = conn.execute(
            "SELECT id, kind, text FROM memory_items WHERE user_id=? ORDER BY id ASC",
            (uid,),
        ).fetchall()
        conn.close()
    return {"ok": True, "items": [dict(r) for r in rows]}


@app.get("/api/history")
def history(user_id: str, response: Response):
    if user_id.strip():
        _set_uid_cookie(response, user_id.strip())
    with db_lock:
        conn = _conn()
        rows = conn.execute(
            "SELECT role, content, created_at FROM messages WHERE user_id=? ORDER BY id ASC",
            (user_id,),
        ).fetchall()
        s = conn.execute(
            "SELECT summary FROM summaries WHERE user_id=?", (user_id,)
        ).fetchone()
        u = conn.execute(
            "SELECT crisis_mode, crisis_banner, crisis_lang FROM users WHERE id=?", (user_id,)
        ).fetchone()
        rh = conn.execute(
            "SELECT id, person_type, person_name, relationship, topic, "
            "debrief, created_at FROM rehearsals WHERE user_id=? "
            "AND state='debriefed' ORDER BY id ASC",
            (user_id,),
        ).fetchall()
        conn.close()
    rehearsals = []
    for r in rh:
        d = dict(r)
        try:
            d["debrief"] = json.loads(d["debrief"] or "null")
        except Exception:
            d["debrief"] = None
        d["person_label"] = _rehearsal_person_label(r)
        d["person_display"] = _persona_display(r)
        rehearsals.append(d)
    return {
        "messages": [dict(r) for r in rows],
        "summary": s["summary"] if s else "",
        "crisis": bool(u["crisis_mode"]) if u else False,
        "banner": bool(u["crisis_banner"]) if u else False,
        "banner_lang": (u["crisis_lang"] or "en") if u else "en",
        "rehearsals": rehearsals,
    }


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/health")
def health_alias():
    """Keep-alive target for an external pinger (Tab ruling): 200,
    instant, no model call, no DB work."""
    return {"ok": True}


# --------------------------------------- Anonymous usage stats ----
# Counts only — never content. Visitors are recorded under a
# one-way SHA-256 of their browser id with a stats-only salt that is
# generated on the server and lives only inside this separate
# database file, so a stats row can never be joined back to anyone's
# chats, summaries or memory. Rows hold only a hashed id, a day, and
# counters: no message text, no IPs, no user agents, nothing finer
# than the day. Every writer is fire-and-forget — stats can never
# slow or break a chat or a rehearsal.
STATS_DB_PATH = os.path.join(DATA_DIR, "stats.db")
_stats_lock = threading.Lock()
_stats_ready_paths = set()
_stats_salt_cache = {}


def _stats_iso_day():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


def _stats_ensure(path=None):
    path = path or STATS_DB_PATH
    if path in _stats_ready_paths:
        return path
    with _stats_lock:
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS stat_meta (
                    k TEXT PRIMARY KEY, v TEXT);
                CREATE TABLE IF NOT EXISTS stat_visitors (
                    hid TEXT PRIMARY KEY, first_seen TEXT);
                CREATE TABLE IF NOT EXISTS stat_activity (
                    hid TEXT, day TEXT, PRIMARY KEY (hid, day));
                CREATE TABLE IF NOT EXISTS stat_features (
                    hid TEXT, feature TEXT, PRIMARY KEY (hid, feature));
                CREATE TABLE IF NOT EXISTS stat_votes (
                    surface TEXT, vote INTEGER, n INTEGER DEFAULT 0,
                    PRIMARY KEY (surface, vote));
                """
            )
            row = conn.execute(
                "SELECT v FROM stat_meta WHERE k = 'salt'").fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO stat_meta (k, v) VALUES ('salt', ?)",
                    (secrets.token_hex(32),))
                conn.commit()
            _stats_salt_cache[path] = conn.execute(
                "SELECT v FROM stat_meta WHERE k = 'salt'"
            ).fetchone()[0]
        finally:
            conn.close()
    _stats_ready_paths.add(path)
    return path


def _stats_hid(uid, path=None):
    path = _stats_ensure(path)
    salt = _stats_salt_cache.get(path, "")
    return hashlib.sha256(
        (salt + ":" + str(uid)).encode("utf-8")).hexdigest()


def stats_touch(uid, day=None):
    """Record a visitor as active on a day (first-seen day = the
    new-user cohort). Fire-and-forget."""
    try:
        if not uid:
            return
        path = _stats_ensure()
        day = day or _stats_iso_day()
        hid = _stats_hid(uid, path)
        with _stats_lock:
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO stat_visitors (hid, first_seen)"
                    " VALUES (?, ?)", (hid, day))
                conn.execute(
                    "INSERT OR IGNORE INTO stat_activity (hid, day)"
                    " VALUES (?, ?)", (hid, day))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def stats_feature(uid, feature, day=None):
    """Record that a visitor used a feature (once per visitor)."""
    try:
        if not uid or not feature:
            return
        stats_touch(uid, day)
        path = _stats_ensure()
        hid = _stats_hid(uid, path)
        with _stats_lock:
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO stat_features (hid, feature)"
                    " VALUES (?, ?)", (hid, feature))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def stats_vote(surface, vote):
    """Aggregate helpfulness counts — no identity is stored."""
    try:
        path = _stats_ensure()
        with _stats_lock:
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "INSERT INTO stat_votes (surface, vote, n)"
                    " VALUES (?, ?, 1) ON CONFLICT (surface, vote)"
                    " DO UPDATE SET n = n + 1",
                    (surface, int(vote)))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def _stats_report():
    """Structured counts for the private stats page: new users per
    day (last 30), cohort returns at day +7 / +30, feature users,
    helpfulness vote totals."""
    from datetime import date, timedelta
    path = _stats_ensure()
    today = date.fromisoformat(_stats_iso_day())
    conn = sqlite3.connect(path)
    try:
        new_rows = conn.execute(
            "SELECT first_seen, COUNT(*) FROM stat_visitors"
            " GROUP BY first_seen ORDER BY first_seen DESC LIMIT 30"
        ).fetchall()
        cohorts = []
        for day0, size in new_rows:
            d0 = date.fromisoformat(day0)

            def _back(off):
                target = (d0 + timedelta(days=off)).isoformat()
                return conn.execute(
                    "SELECT COUNT(*) FROM stat_activity a"
                    " JOIN stat_visitors v ON v.hid = a.hid"
                    " WHERE v.first_seen = ? AND a.day = ?",
                    (day0, target)).fetchone()[0]

            cohorts.append({
                "day": day0, "size": size,
                "d7": _back(7) if d0 + timedelta(days=7) <= today else None,
                "d30": _back(30) if d0 + timedelta(days=30) <= today else None,
            })
        feats = dict(conn.execute(
            "SELECT feature, COUNT(*) FROM stat_features"
            " GROUP BY feature").fetchall())
        votes = {}
        for surface, vote, n in conn.execute(
                "SELECT surface, vote, n FROM stat_votes"):
            votes.setdefault(surface, {})[int(vote)] = n
        return {
            "new_by_day": [{"day": d, "n": n} for d, n in new_rows],
            "cohorts": cohorts,
            "rehearse_users": feats.get("rehearse", 0),
            "voice_users": feats.get("voice", 0),
            "votes": votes,
        }
    finally:
        conn.close()


class StatsEventIn(BaseModel):
    user_id: str = ""
    event: str = ""


@app.post("/api/stats/event")
def stats_event(body: StatsEventIn):
    # Only "visit" is recorded from the client; feature use is
    # recorded server-side where it actually happens.
    if body.event == "visit" and body.user_id:
        stats_touch(body.user_id)
    return {"ok": True}


class StatsVoteIn(BaseModel):
    surface: str = ""
    vote: int = 0


@app.post("/api/stats/vote")
def stats_vote_ep(body: StatsVoteIn):
    # Counts only — no identity is read or stored for a vote; the
    # once-per-session limit lives on the client.
    if body.surface in ("chat", "rehearse") and body.vote in (1, -1):
        stats_vote(body.surface, body.vote)
    return {"ok": True}


@app.get("/stats", response_class=HTMLResponse)
def stats_page(key: str = ""):
    want = os.environ.get("STATS_KEY", "")
    if not want or not key or not secrets.compare_digest(key, want):
        # Indistinguishable from any other missing page.
        raise HTTPException(status_code=404, detail="Not found")
    rep = _stats_report()

    def _rows(items, fmt):
        return "".join(fmt(i) for i in items) or (
            '<tr><td colspan="4">No data yet.</td></tr>')

    new_rows = _rows(rep["new_by_day"], lambda r:
                     "<tr><td>%s</td><td>%d</td></tr>"
                     % (html.escape(r["day"]), r["n"]))
    cohort_rows = _rows(rep["cohorts"], lambda c:
                         "<tr><td>%s</td><td>%d</td><td>%s</td><td>%s</td></tr>"
                         % (html.escape(c["day"]), c["size"],
                            "—" if c["d7"] is None else c["d7"],
                            "—" if c["d30"] is None else c["d30"]))
    votes = rep["votes"]
    up = sum(v.get(1, 0) for v in votes.values())
    down = sum(v.get(-1, 0) for v in votes.values())

    def _v(surface, vote):
        return votes.get(surface, {}).get(vote, 0)

    return HTMLResponse(
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Solace — usage counts</title><style>"
        "body{font-family:-apple-system,system-ui,sans-serif;max-width:640px;"
        "margin:0 auto;padding:24px 18px 60px;color:#2b2620;background:#faf8f4;"
        "line-height:1.6} table{border-collapse:collapse;margin:8px 0 24px}"
        "td,th{border:1px solid #ddd5c6;padding:5px 12px;text-align:left}"
        "h2{font-size:17px;margin-top:28px} .note{color:#8a8178;font-size:13px}"
        "</style></head><body>"
        "<h1>Solace — usage counts</h1>"
        "<p class=\"note\">Counts only. No message text, summaries or memory "
        "are ever part of these numbers, and visitor ids here are one-way "
        "hashes that cannot be matched back to anyone's chats.</p>"
        "<h2>New users per day (last 30 days)</h2>"
        "<table><tr><th>Day</th><th>New users</th></tr>" + new_rows + "</table>"
        "<h2>Returns by cohort</h2>"
        "<table><tr><th>First day</th><th>New users</th>"
        "<th>Back on day +7</th><th>Back on day +30</th></tr>"
        + cohort_rows + "</table>"
        "<h2>Features</h2>"
        "<table>"
        "<tr><td>Rehearse users</td><td>%d</td></tr>"
        "<tr><td>Voice Rehearse users</td><td>%d</td></tr>"
        "</table>"
        "<h2>“Did this help?”</h2>"
        "<table>"
        "<tr><th></th><th>👍</th><th>👎</th></tr>"
        "<tr><td>After a rehearsal</td><td>%d</td><td>%d</td></tr>"
        "<tr><td>In the main chat</td><td>%d</td><td>%d</td></tr>"
        "<tr><td><strong>Total</strong></td><td><strong>%d</strong></td>"
        "<td><strong>%d</strong></td></tr>"
        "</table>"
        "</body></html>" % (
            rep["rehearse_users"], rep["voice_users"],
            _v("rehearse", 1), _v("rehearse", -1),
            _v("chat", 1), _v("chat", -1), up, down))


# ------------------------------------------------------ Frontend ----
app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "static")), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(ROOT, "static", "index.html"))


@app.get("/privacy")
def privacy():
    return FileResponse(os.path.join(ROOT, "static", "privacy.html"))


@app.get("/terms")
def terms():
    return FileResponse(os.path.join(ROOT, "static", "terms.html"))

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
    # Tab FINAL order A2: a stated plan / intent counts even
    # without method words — "ending it tonight", "ending it
    # all", "thinking of ending it … I have a plan". ("Ending
    # it" about a relationship carries none of these frames.)
    r"ending it all",
    r"(?:ending|end) it (?:tonight|today|soon)\b",
    r"(?:thinking of|planning to|going to) (?:ending|end) it\b",
    r"לסיים את זה (?:הלילה|היום)",
    r"חושב לסיים את זה",
    # Tab FINAL order A1.3 / Part B: the idiom word tied to life
    # or death in the same message IS a crisis statement.
    r"better off without me",
    r"(?:go to sleep|fall asleep|sleep) and never wake",
    r"wish i could\b[^.!?\n]{0,40}\bnever wake",
    r"bought a rope\b[^.!?\n]{0,50}\bknow what (?:it'?s|its) for\b",
    r"נמאס לי (?:מ)?לחיות",
    r"נמאס לי מהחיים",
    r"יותר טובים? בלעדיי?",
    r"לגמור עם עצמי",
    r"להתאבר",
    r"להתבד",
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
    # Tab r13 item 1: the "be alive" family was missing, so
    # "I don't want to be alive anymore" fell through to the
    # (slow, probabilistic) classifier and took 0.6-2.6s on
    # device. Every one of these is deterministic now.
    r"don'?t want to be alive",
    r"do not want to be alive",
    r"don'?t wanna be alive",
    r"not want to be alive",
    r"want to be dead",
    r"rather be dead",
    r"no (?:will|desire) to live",
    r"sick of (?:living|being alive)",
    r"can'?t stand (?:living|being alive)",
    r"end my own life",
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
    # Tab r13 item 1: the Hebrew "be alive" family joins the
    # deterministic layer (was classifier-only, like English).
    r"לא רוצה להיות בחיים",
    r"נמאס לי להיות בחיים",
    r"רוצה להיות מת",
    r"הלוואי (?:ש)?הייתי מת",
    r"לא רוצה להתקיים",
    r"מעדיף למות",
    r"רוצה להרוג את עצמי",
    r"לשים קץ לחיים",
    r"לגמור עם החיים",
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
    "טוב שסיפרת לי. חשוב לי לוודא שאתה בטוח עכשיו. "
    "האם אתה חושב לפעול על פי המחשבות האלה הלילה? אתה לא צריך לעבור את זה לבד — "
    "בישראל אפשר להתקשר לער״ן, קו הסיוע הנפשי, במספר 1201 (זמין 24/7). "
    "בארה״ב אפשר להתקשר או לשלוח הודעה ל־988 (קו החיים למניעת התאבדות, 24/7), "
    "או לשלוח HOME ל־741741. אם אתה בסכנה מיידית, התקשר למד״א במספר 101. "
    "אני כאן איתך — אתה רוצה לספר לי מה עובר עליך?"
)

# =====================================================================
# Tab FINAL ORDER (Oct 27) — the deterministic layer this order adds.
# Where it conflicts with earlier rounds, THIS section governs.
# =====================================================================

# ---- A5/A14: the Hebrew crisis reply in three writer-gender
# versions. A14 rewrote the opening to «טוב שסיפרת לי» (the old
# "אני ממש שמח שסיפרת לי על זה" is gone — the order supersedes
# the frozen HE string; the EN frozen string stays byte-
# identical, and the suites' HE hash pin moves to the new
# masculine version). The feminine and neutral versions carry
# the same content with the writer addressed in their own
# gender — or, while no gender is known, in forms that address
# no gender at all (no אתה and no את forms toward the writer).
FIXED_CRISIS_RESPONSE_HE_F = (
    "טוב שסיפרת לי. חשוב לי לוודא שאת בטוחה עכשיו. "
    "האם את חושבת לפעול על פי המחשבות האלה הלילה? את לא צריכה לעבור את זה לבד — "
    "בישראל אפשר להתקשר לער״ן, קו הסיוע הנפשי, במספר 1201 (זמין 24/7). "
    "בארה״ב אפשר להתקשר או לשלוח הודעה ל־988 (קו החיים למניעת התאבדות, 24/7), "
    "או לשלוח HOME ל־741741. אם את בסכנה מיידית, התקשרי למד״א במספר 101. "
    "אני כאן איתך — את רוצה לספר לי מה עובר עלייך?"
)
FIXED_CRISIS_RESPONSE_HE_N = (
    "טוב שסיפרת לי. חשוב לי לוודא שהכול בטוח עכשיו. "
    "האם יש מחשבות לפעול על פי המחשבות האלה הלילה? לא צריך לעבור את זה לבד — "
    "בישראל אפשר להתקשר לער״ן, קו הסיוע הנפשי, במספר 1201 (זמין 24/7). "
    "בארה״ב אפשר להתקשר או לשלוח הודעה ל־988 (קו החיים למניעת התאבדות, 24/7), "
    "או לשלוח HOME ל־741741. אם יש סכנה מיידית, צריך להתקשר למד״א במספר 101. "
    "אני כאן — אפשר לספר לי מה עובר עכשיו?"
)


def crisis_reply_he(gender):
    """The fixed Hebrew crisis reply for the writer's stored
    gender (A5): 'f' feminine, 'm' masculine (the frozen string),
    anything else the neutral version."""
    if gender == "f":
        return FIXED_CRISIS_RESPONSE_HE_F
    if gender == "m":
        return FIXED_CRISIS_RESPONSE_HE
    return FIXED_CRISIS_RESPONSE_HE_N


# ---- A4: normalization used by every new detector below. Matching
# only — the person's own words are never rewritten by it.
_NIQQUD_RE = re.compile(r"[\u0591-\u05C7]")
_REPEAT_LETTER_RE = re.compile(r"([a-zA-Z\u0590-\u05FF])\1{2,}")


def _norm_match(text):
    """Lowercase, niqqud stripped, punctuation folded to spaces,
    repeated letters collapsed, slang expanded (kms / unalive) —
    so a typo, a stretched letter or a slang spelling of a crisis
    word still matches the detectors (A4)."""
    t = _NIQQUD_RE.sub("", (text or "").lower())
    t = (t.replace("״", '"').replace("׳", "'")
         .replace("’", "'").replace("‘", "'"))
    t = _REPEAT_LETTER_RE.sub(r"\1", t)
    t = re.sub(r"[^\w\s']", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\bkms\b", "kill myself", t)
    t = re.sub(r"\bkys\b", "kill yourself", t)
    t = re.sub(r"\bunalive\s+myself\b", "kill myself", t)
    t = re.sub(r"\bunalive\b", "suicide", t)
    # A4: one-letter typos on the crisis keywords themselves
    # (kill / kil, myself / myslef, want / wnat, suicide /
    # suiside) still match the deterministic check.
    t = re.sub(r"\bkil\b(?=\s+(?:myself|me|himself|herself))",
               "kill", t)
    t = re.sub(r"\bmyslef\b", "myself", t)
    t = re.sub(r"\bwnat\b", "want", t)
    t = re.sub(r"\bsuiside\b", "suicide", t)
    t = re.sub(r"\bcomit\b", "commit", t)
    return t


# ---- A1: NEVER-CRISIS idioms. Checked BEFORE the classifier (and
# before the keyword layer's verdict is trusted): a message that is
# plainly an idiom, a joke frame, a song lyric, a quote, or a
# school / work topic is NOT a crisis, however dark its words are.
# Crisis turns on only on a stated intent, wish, plan or means of
# self-harm in the person's own voice.
_NEVER_CRISIS_RES = [
    # English idiom / hyperbole frames
    re.compile(r"\bdying\s+(?:of\s+)?(?:laughter|laughing)\b"),
    re.compile(r"\bi(?:'m| am)\s+dying\s+(?:here\s+)?(?:laughing|of\s+laughter)\b"),
    re.compile(r"\bwant\s+to\s+die\s+laughing\b"),
    re.compile(r"\bgoing\s+to\s+die\s+laughing\b"),
    re.compile(r"\bthis\s+(?:song|track|beat|video)\s+is\s+killing\s+me\b"),
    re.compile(r"\bkill(?:ing)?\s+me\s+(?:laughing|with\s+laughter)\b"),
    re.compile(r"\b(?:could|would)\s+kill\s+for\s+(?:a|an|some)\b"),
    re.compile(r"\bkill\s+the\s+lights\b"),
    re.compile(r"\bdead\s+tired\b"),
    # "can't take it anymore" tied to a mundane external cause
    # ("with this noise upstairs") is overwhelm about the cause,
    # not a self-harm statement (Part B line 33). Normalization
    # strips punctuation, so the cause is a short word run that
    # stops before any word that could open a crisis clause —
    # a real crisis clause in the same message still fires the
    # keyword layer on the rest; "with my life / myself" is
    # self-referential and excluded.
    re.compile(r"\bcan'?t\s+take\s+(?:it|this)\s+anymore\s+with\s+"
               r"(?:this|the|my)\b(?!\s*(?:life|myself)\b)"
               r"(?:\s+(?!i\b|want\b|die\b|kill\b|end\b|hurt\b|"
               r"myself\b)[a-z]+){0,4}"),
    re.compile(r"\bdrop[-\s]?dead\s+gorgeous\b"),
    re.compile(r"\bi(?:'m| am)\s+(?:so\s+)?dead\s+(?:meat|tired)\b"),
    # A1's explicit list: drowning (in work), at the end of my
    # rope, bury myself (in shame / work), burnt out, disappear
    # or run away FOR A BREAK — never a crisis on their own.
    re.compile(r"\bdrowning\s+in\s+(?:work|emails?|paperwork|debt|"
               r"tasks|homework)\b"),
    re.compile(r"\bat\s+the\s+end\s+of\s+my\s+rope\s+with\b"),
    re.compile(r"\bbury\s+myself\s+(?:in\s+work|from\s+shame|"
               r"in\s+shame)\b"),
    re.compile(r"\bburnt\s+out\b|\bburned\s+out\b"),
    re.compile(r"\bdisappear\s+for\s+(?:the\s+)?(?:weekend|a\s+few\s+"
               r"days|a\s+while|a\s+bit)\b"),
    re.compile(r"\brun\s+away\s+from\s+(?:it\s+all|everything)\s+for\s+"
               r"(?:a\s+few\s+days|a\s+while|the\s+weekend)\b"),
    # joke / lyric / quote / school frames around dark words
    re.compile(r"\b(?:lyric|song|quote|quoting|homework|essay|assignment|"
               r"schoolwork|book report)\b[^.!?\n]{0,60}?"
               r"\b(?:suicid\w*|kill\s+(?:myself|himself|herself|themselves)|die)\b"),
    re.compile(r"\b(?:suicid\w*|kill\s+myself)\b[^.!?\n]{0,60}?"
               r"\b(?:lyric|song|quote|homework|essay|assignment|school)\b"),
    re.compile(r"\bit\s+was\s+a\s+joke\b|\bi\s+was\s+joking\b|"
               r"\bjust\s+joking\b|\bjust\s+kidding\b"),
    # Hebrew idiom frames: laughing to death, "I'm finished" as
    # overwhelm, and the same joke / school frames.
    re.compile(r"(?:אני\s+)?מת\s+(?:מ)?צחוק"),
    re.compile(r"מרגיש\s+שאני\s+מת\s+מצחוק"),
    re.compile(r"כשאני\s+צוחק\s+אני\s+מרגיש\s+שאני\s+מת"),
    re.compile(r"אני\s+גמור\s*$|קשה\s+לי\s+אני\s+גמור"),
    # A1's Hebrew list: מת מעייפות (dead tired), מת מבושה,
    # לקבור את עצמי מבושה, בא לי להיעלם / לברוח להפסקה,
    # נמאס לי (alone), אין לי כוח (for a person/thing),
    # טובע בעבודה, מרגיש על הפנים, המבחן הורג אותי.
    re.compile(r"מת\s+(?:מ)?(?:עייפות|מבושה|בושה)"),
    re.compile(r"לקבור\s+את\s+עצמי\s+מבושה"),
    re.compile(r"בא\s+לי\s+להיעלם\s+(?:לסוף|לכמה|לקצת)"),
    re.compile(r"בא\s+לי\s+לברוח\s+מהכל\s+לכמה\s+ימים"),
    re.compile(r"נמאס\s+לי\s*$"),
    re.compile(r"אין\s+לי\s+כוח\s+יותר\s+ל"),
    re.compile(r"טובע\s+ב(?:עבודה|מיילים|משימות)"),
    re.compile(r"מרגיש\s+על\s+הפנים"),
    re.compile(r"אני\s+שבור\s*$"),
    re.compile(r"(?:המבחן|העבודה|השיעור|החום|הרעש)\s+הזה\s+הורג\s+אותי"),
    re.compile(r"(?:שיר|מילים\s+של\s+שיר|ציטוט|שיעורי\s+בית|עבודה\s+לבית\s+הספר|חיבור)"
               r"[^.!?\n]{0,40}?(?:להתאבד|התאבדות|למות|להרוג\s+את\s+עצמי)"),
    re.compile(r"(?:זה\s+היה\s+)?בצחוק|רק\s+צחקתי|סתם\s+צחקתי"),
]


def is_never_crisis_idiom(text):
    """A1: True when the message is plainly an idiom / joke / lyric /
    quote / school frame — never a crisis, whatever its words.
    The idiom must account for the dark words: if crisis keywords
    remain OUTSIDE the idiom's span, the message is not cleared."""
    n = _norm_match(text)
    matched = False
    rest = n
    for rx in _NEVER_CRISIS_RES:
        if rx.search(rest):
            matched = True
            rest = rx.sub(" ", rest)
    if not matched:
        return False
    return not detect_crisis_keywords(rest)


# ---- A3: MEDICAL EMERGENCY — a deterministic check of its own,
# run BEFORE the suicide check. First line is the call to 911 /
# מד״א 101; no "tonight?" question, banner on, under a second
# (no model call anywhere near it).
_MEDICAL_RES = [
    # can't breathe / choking
    re.compile(r"\bcan'?t\s+breathe\b|\bcannot\s+breathe\b|"
               r"\bstruggling\s+to\s+breathe\b|\bi'?m\s+choking\b"),
    re.compile(r"לא\s+(?:מצליח|יכול)\s+לנשום|קשה\s+לי\s+לנשום|אני\s+נחנק"),
    # Tab r13 item 2: THIRD-PERSON emergencies. "My dad
    # collapsed and isn't breathing" matched NOTHING here —
    # collapsed / not-breathing (about someone else) were
    # simply absent, so the turn fell through to the model.
    re.compile(r"\bcollaps\w*\b"),
    re.compile(r"\bnot\s+breathing\b|\bisn'?t\s+breathing\b|"
               r"\bstopped\s+breathing\b|\bno\s+longer\s+breathing\b|"
               r"\bisn'?t\s+breathing\s+(?:anymore|any\s+more)\b"),
    re.compile(r"\bno\s+pulse\b|\bnot\s+responsive\b|"
               r"\bunresponsive\b|\bheart\s+attack\b|\bstroke\b|"
               r"\bchoking\b|\bturning\s+blue\b|"
               r"\blips?\s+(?:are\s+)?(?:turning\s+)?blue\b"),
    re.compile(r"התמוטט|התמוטטה|לא\s+נושם|לא\s+נושמת|"
               r"הפסיק\s+לנשום|הפסיקה\s+לנשום|אין\s+נשימה|"
               r"איבד\s+(?:את\s+)?ההכרה|איבדה\s+(?:את\s+)?ההכרה|"
               r"אובדן\s+הכרה|אין\s+דופק|"
               r"התקף\s+לב|שבץ\s+מוחי|נחנק|נחנקת"),
    # chest pain
    re.compile(r"\bchest\s+pain\b|\bpain\s+in\s+my\s+chest\b|"
               r"\bchest\s+(?:hurts|is\s+tight)\b"),
    re.compile(r"כאב\s+(?:ב)?חזה|כאבים\s+בחזה|לחץ\s+בחזה"),
    # unconscious / won't wake
    re.compile(r"\bunconscious\b|\bpassed\s+out\b|"
               r"\bwon'?t\s+wake\s+up\b|\bnot\s+waking\s+up\b|"
               r"\bisn'?t\s+waking\s+up\b"),
    re.compile(r"מחוסר\s+הכרה|לא\s+מתעורר|התעלף|התעלפה"),
    # seizure
    re.compile(r"\bseizure\b|\bconvuls"),
    re.compile(r"פרכוס|עוויתות"),
    # heavy bleeding
    re.compile(r"\bbleeding\s+(?:heavily|badly|a\s+lot)\b|"
               r"\bwon'?t\s+stop\s+bleeding\b|\bblood\s+everywhere\b|"
               r"\bbleeding\s+out\b|\blosing\s+(?:a\s+lot\s+of\s+)?blood\b|"
               r"\bblood\s+(?:is\s+)?(?:pouring|gushing)\b"),
    # Tab r13 item 2: Hebrew heavy bleeding — the standard
    # term is דימום כבד, which was missing entirely, along with
    # the feminine and possessive forms; a Hebrew bleeding
    # message reached the suicide script instead of מד״א 101.
    re.compile(r"דימום\s+(?:חזק|כבד|מסיבי)|מדמם\s+הרבה|"
               r"הדימום\s+לא\s+עוצר|מדממת|מדמם\s+(?:המון|בלי\s+הפסקה)|"
               r"(?:הרבה|המון)\s+דם|דם\s+(?:בכל\s+מקום|לא\s+מפסיק)|"
               r"שופך\s+דם|מאבד\s+(?:הרבה\s+)?דם|ממשיך\s+לדמם|"
               r"לא\s+מפסיק\s+לדמם|הדם\s+לא\s+מפסיק"),
    # severe allergic reaction
    re.compile(r"\ballergic\s+reaction\b|\banaphylax|"
               r"\bthroat\s+is\s+closing\b|\blips?\s+(?:are\s+)?swelling\b"),
    re.compile(r"תגובה\s+אלרגית|הגרון\s+נסגר|השפתיים\s+מתנפחות"),
    # Tab r14 item 2: anaphylaxis shapes Tab named — throat or
    # tongue swelling, trouble breathing (after food or a sting:
    # the symptom is what routes, whatever caused it).
    re.compile(r"\b(?:throat|tongue)\s+(?:is\s+)?(?:swelling|"
               r"swollen|closing\s+up)\b|"
               r"\bswollen\s+(?:throat|tongue)\b|"
               r"\bmy\s+(?:throat|tongue)\s+(?:is\s+)?swell\w*\b|"
               r"\btrouble\s+breathing\b|\bdifficulty\s+breathing\b|"
               r"\bhard\s+to\s+breathe\b|"
               r"\bcan'?t\s+breathe\b|\bcannot\s+breathe\b"),
    re.compile(r"הגרון\s+(?:שלי\s+)?מתנפח|הלשון\s+(?:שלי\s+)?מתנפחת|"
               r"נפיחות\s+ב(?:גרון|לשון)|גרון\s+נפוח|לשון\s+נפוחה|"
               r"קשיי\s+נשימה|מתקשה\s+לנשום|"
               r"קשה\s+לי\s+לנשום|לא\s+(?:יכול|מצליח)(?:ה)?\s+לנשום|"
               r"התנפח\s+לי\s+הגרון|התנפחה\s+לי\s+הלשון"),
    # Tab r14 item 2: bleeding that "won't stop" — the EN list
    # knew "won't stop bleeding" but not "the bleeding won't
    # stop" / "can't stop the bleeding"; the HE list gains the
    # לא מפסיק form for the bleeding noun itself.
    re.compile(r"\bbleeding\s+(?:won'?t|will\s+not|doesn'?t|"
               r"isn'?t|is\s+not)\s+stop\b|"
               r"\bcan'?t\s+stop\s+the\s+bleeding\b|"
               r"\bcannot\s+stop\s+the\s+bleeding\b|"
               r"\bkeeps\s+bleeding\b"),
    re.compile(r"הדימום\s+לא\s+מפסיק|דימום\s+שלא\s+עוצר|"
               r"הדימום\s+ממשיך"),
    # the word overdose — in a COMPLETED frame only. A plan
    # ("I'm going to overdose tonight") keeps the frozen crisis
    # path (Tab's standing ruling); a completed ingestion is
    # also caught by detect_overdose below.
    re.compile(r"\boverdosed\b|\btook\s+an?\s+overdose\b|"
               r"\boverdosing\b|"
               r"\boverdose\b[^.!?\n]{0,30}\bago\b"),
    re.compile(r"הרעלת\s+תרופות"),
]


def detect_medical_emergency(text):
    """A3: 'ingestion' for a completed ingestion / overdose,
    'medical' for another medical emergency, else None. Runs on
    the normalized text so typos and slang cannot slip past it.
    A plain prescribed-medication statement is guarded out by the
    caller, exactly as on the overdose path."""
    n = _norm_match(text)
    if any(rx.search(n) for rx in _MEDICAL_RES):
        # An ingestion word rides the medical path too — the
        # Poison Control line joins the reply below. Ingestion
        # includes a third person ("my friend took too many
        # pills and won't wake up" — Part B line 22).
        if re.search(r"\boverdose\b|\boverdosed\b", n) \
                or "מנת יתר" in n or "הרעלת תרופות" in n \
                or re.search(r"\btoo\s+many\s+pills\b", n) \
                or re.search(
                    r"\b(?:took|swallowed|ate|drank|taken)\b"
                    r"[^.!?\n]{0,40}?\b(?:pills?|bottle|tylenol|"
                    r"tablets?|capsules?|meds|medicine|sleeping\s+"
                    r"pills)\b", n):
            return "ingestion"
        return "medical"
    if detect_overdose(text):
        return "ingestion"
    return None


MEDICAL_RESPONSE = (
    "Call 911 right now — this sounds like a medical emergency. "
    "Please make that call now; I'll be here when you're able to "
    "come back."
)
MEDICAL_RESPONSE_INGESTION = (
    "Call 911 right now — this sounds like a medical emergency. "
    "You can also call Poison Control at 1-800-222-1222. Please "
    "call now; I'll be here when you're able to come back."
)
_MEDICAL_HE = {
    "m": (
        "התקשר למד״א במספר 101 עכשיו ממש — זה נשמע כמו מצב חירום "
        "רפואי. בבקשה להתקשר עכשיו — אני כאן כשאפשר יהיה לחזור."
    ),
    "f": (
        "התקשרי למד״א במספר 101 עכשיו ממש — זה נשמע כמו מצב חירום "
        "רפואי. בבקשה להתקשר עכשיו — אני כאן כשאפשר יהיה לחזור."
    ),
    "n": (
        "צריך להתקשר למד״א במספר 101 עכשיו ממש — זה נשמע כמו מצב "
        "חירום רפואי. בבקשה להתקשר עכשיו — אני כאן כשאפשר יהיה לחזור."
    ),
}


def medical_reply(kind, hebrew, gender):
    if hebrew:
        return _MEDICAL_HE.get(gender or "", _MEDICAL_HE["n"])
    return (MEDICAL_RESPONSE_INGESTION if kind == "ingestion"
            else MEDICAL_RESPONSE)


# ---- A2: clarification / denial. When a latched person clarifies
# ("I was joking", "I don't want to die", "I'm safe"), the latch
# and the banner come OFF and the reply is this brief fixed line —
# no check-in question, no crisis text.
CLARIFY_REPLY = (
    "Thank you for telling me — I'm glad to hear that. "
    "I'm here whenever you want to talk."
)
_CLARIFY_HE = {
    "m": "טוב שסיפרת לי — אני שמח לשמוע. אני כאן מתי שתרצה לדבר.",
    "f": "טוב שסיפרת לי — אני שמח לשמוע. אני כאן מתי שתרצי לדבר.",
    "n": "טוב שסיפרת לי — אני שמח לשמוע. אני כאן בכל זמן שמתאים לדבר.",
}

# The ONE gentle check-in owed on the turn after a true crisis
# (A2): prepended by code to that turn's normal reply, once.
CHECKIN_LINE = "I'm glad you're here — how are you doing right now?"
_CHECKIN_HE = {
    "m": "אני שמח שאנחנו מדברים — איך אתה מרגיש עכשיו?",
    "f": "אני שמח שאנחנו מדברים — איך את מרגישה עכשיו?",
    "n": "אני שמח שאנחנו מדברים — איך ההרגשה עכשיו?",
}

_CLARIFY_RES = [
    re.compile(r"\bi\s+(?:was|am)\s+(?:just\s+)?joking\b|"
               r"\bi\s+was\s+kidding\b|\bjust\s+kidding\b", re.I),
    re.compile(r"\bi\s+don'?t\s+(?:mean|want)\s+"
               r"(?:it|to\s+die|that)\b", re.I),
    re.compile(r"\bi\s+didn'?t\s+mean\s+it\b", re.I),
    re.compile(r"\bi(?:'m| am)\s+not\s+(?:suicidal|going\s+to\s+"
               r"(?:hurt|kill)\s+myself)\b", re.I),
    re.compile(r"\bi\s+won'?t\s+(?:do\s+anything|hurt\s+myself)\b",
               re.I),
    re.compile(r"\bi(?:'m| am)\s+(?:safe|fine|okay|ok)\b", re.I),
    re.compile(r"\bnot\s+like\s+that\b", re.I),
    re.compile(r"\bjust\s+venting\b|\bi\s+meant\b[^.!?\n]{0,30}?"
               r"\b(?:tired|exhausted|joking)\b", re.I),
    re.compile(r"אני\s+לא\s+מתכוון|לא\s+באמת|אני\s+בטוח|"
               r"אני\s+לא\s+אעשה\s+(?:כלום|שום\s+דבר)|"
               r"התכוונתי\s+בצחוק|אני\s+בסדר"),
    # A2's own example: "התכוונתי ש…" ("I meant that…") — the
    # walk-back that restates the words as something ordinary.
    re.compile(r"התכוונתי\s+ש"),
    re.compile(r"סליחה\s+התכוונתי"),
]


def detect_clarification(text):
    """A2: the person walks the crisis back — a joke frame, a
    denial of intent, or an explicit 'I'm safe / I'm fine'."""
    if detect_retraction(text):
        return True
    n = _norm_match(text)
    return any(rx.search(n) for rx in _CLARIFY_RES)


# A latched turn that is only a fragment or a continuation of the
# crisis ("I don't know what to do", "ok", "yes") stays IN the
# crisis flow; anything with real new content is the person moving
# to a new topic (A2), which clears the latch.
_LATCH_FRAGMENT_RES = [
    re.compile(r"^\s*(?:i\s+don'?t\s+know.*|i\s+dunno|ok|okay|yes|"
               r"yeah|no|nope|maybe|i\s+guess.*|help.*|please.*|"
               r"thanks?.*|thank\s+you.*)\s*[.!?]*\s*$", re.I),
    re.compile(r"^\s*(?:אני\s+לא\s+יודע.*|לא\s+יודע.*|טוב|כן|לא|"
               r"אולי|עזרה.*|בבקשה.*|תודה.*)\s*[.!?…]*\s*$"),
]


def _is_latch_fragment(text):
    n = (text or "").strip()
    if len(n.split()) <= 2:
        return True
    return any(rx.search(n) for rx in _LATCH_FRAGMENT_RES)


# ---- A13: a content-free greeting gets the TEMPLATE reply — the
# person's name when known, the ONE fixed self-description, and
# one open question. Never a model improvisation about the person.
_GREETING_RE = re.compile(
    r"^\s*(?:hi+|hello|hey+|שלום|היי+|הי|מה\s+קורה|מה\s+נשמע|"
    r"what'?s\s+up|good\s+(?:morning|evening|afternoon))"
    r"(?:\s+(?:there|solace|סולאס))?\s*[.!?]*\s*$", re.I)
# The intake's own opener (A13 names it explicitly): the app
# sends "Hi, I'd like to talk." when the person skips the form —
# it is a content-free greeting and gets the template reply.
_GREETING_INTAKE_RE = re.compile(
    r"^\s*hi,?\s+i'?d\s+like\s+to\s+talk\.?\s*$"
    # Tab r13 item 10: the Hebrew first message the app itself
    # sends on a Hebrew phone — it must hit the same instant
    # template greeting, in Hebrew.
    r"|^\s*(?:היי|שלום)[\s,!.]*אשמח\s+לדבר[\s.!]*$", re.I)

GREETING_SELF_EN = "an AI companion for reflection and self-help"
GREETING_SELF_HE = "מלווה בינה מלאכותית להרהור ולתמיכה עצמית"


def is_content_free_greeting(text):
    t = (text or "").strip()
    return bool(_GREETING_RE.search(t)
                or _GREETING_INTAKE_RE.search(t))


def greeting_reply(name, hebrew, gender):
    if hebrew:
        who = (" " + name) if name else ""
        q = {"m": "מה עובר עליך היום?",
             "f": "מה עובר עלייך היום?",
             "n": "מה חדש היום?"}.get(gender or "", "מה חדש היום?")
        return "שלום%s — אני סולאס, %s. %s" % (who, GREETING_SELF_HE, q)
    who = (" " + name) if name else ""
    return ("Hi%s — I'm Solace, %s. What's on your mind today?"
            % (who, GREETING_SELF_EN))


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
        # A3: ingestion with a quantity — a number + a named
        # substance counts ("I took 30 tylenol an hour ago").
        r"(?:took|swallowed|ate)\b[^.!?\n]{0,20}?"
        r"\d+\s*(?:tylenol|advil|ibuprofen|paracetamol|"
        r"acetaminophen|painkillers?|capsules?|mg)\b",
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
        # A3: a quantity frame — "half a pack of Acamol"
        # (חצי חפיסה של אקמול), a pack, a box, a number.
        r"(?:בלעתי|לקחתי|אכלתי)[^.!?\n]{0,20}?"
        r"(?:חצי\s+חפיסה|חפיסה|חבילה|קופס[הא]|"
        r"\d+\s*(?:כדורים|גלולות|טבליות))",
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


# Prescribed-medication statements (Tab round 4): "לקחתי את
# הכדורים שלי" ("I took my pills") triggered the FULL suicide
# reply in 4 of 9 runs — the regex layer passed it (correctly),
# then the probabilistic classifier escalated it. A plain
# medication-taking statement is now classified NOT-crisis BY
# RULE: the guard is evaluated before the overdose detector and
# before the classifier, and the classifier is skipped for it.
# Any quantity / overdose / risk word anywhere in the message
# disqualifies the statement — safety first.
_MED_STATEMENT_RES = [
    re.compile(
        r"(?:לקחתי|נטלתי|שתיתי)[^.!?\n]{0,25}?(?:את\s+)?"
        r"(?:הכדורים|הגלולות|התרופות|התרופה|הכדור)\s+"
        r"שלי(?![\u0590-\u05FF])"),
    re.compile(
        r"\b(?:i\s+)?took\s+my\s+"
        r"(?:pills?|medication|meds|medicine|tablets?)\b", re.I),
]
_MED_UNSAFE_RE = re.compile(
    r"(הרבה|המון|מלא|יותר\s+מדי|בקבוק|חופן|כל\s+הכדורים|"
    r"כל\s+הגלולות|בלע|למות|להתאבד|לפגוע|לסיים\s+עם\s+זה|"
    r"\btoo\s+many\b|\bhandful\b|\bbottle\b|\boverdose\b|"
    r"\ball\s+(?:the|my)\b|\bkill\b|\bdie\b|\bsuicid)",
    re.I)


def is_prescribed_med_statement(text):
    """True for a plain 'I took my (prescribed) pills/medication'
    statement — no quantity, overdose or risk wording anywhere."""
    t = text or ""
    if _MED_UNSAFE_RE.search(t):
        return False
    return any(rx.search(t) for rx in _MED_STATEMENT_RES)


# "Feeling stuck in life" (Tab round 6): "אני מרגיש תקוע
# בחיים" got the FULL frozen suicide reply in 3 of 4 runs —
# the regex layers passed it (correctly), then the classifier
# escalated an ordinary, non-crisis feeling. Like the
# prescribed-med guard, a plain stuck-in-life statement is
# not-crisis BY RULE: evaluated before the classifier, which
# is skipped for it. The keyword layer still runs first and
# wins — a genuine crisis phrase in the same message routes
# to crisis before this guard is ever consulted — and any
# self-harm / ingestion wording disqualifies the statement.
_STUCK_RES = [
    re.compile(
        r"(?:מרגיש(?:ה)?|אני)[^.!?\n]{0,15}?תקוע(?:ה)?"),
    re.compile(r"תקוע(?:ה)?\s+בחיים"),
    re.compile(r"\bi\s+(?:feel\s+|am\s+feeling\s+|have\s+been\s+"
               r"feeling\s+)?stuck\b", re.I),
    re.compile(r"\bfeel(?:ing)?\s+stuck\b", re.I),
    re.compile(r"\bstuck\s+in\s+(?:my\s+)?life\b", re.I),
]
_STUCK_UNSAFE_RE = re.compile(
    r"(למות|להתאבד|לפגוע\s+בעצמ|לסיים\s+עם\s+זה|לסיים\s+את\s+"
    r"החיים|אין\s+(?:לי\s+)?טעם\s+לחיות|לא\s+רוצה\s+לחיות|"
    r"בלע|יותר\s+מדי\s+כדורים|"
    r"\bkill\b|\bsuicid|\bself[-\s]?harm\b|\bhurt\s+myself\b|"
    r"\bend\s+(?:it|my\s+life)\b|\bi\s+want\s+to\s+die\b)",
    re.I)


def is_stuck_in_life_statement(text):
    """True for a plain 'I feel stuck (in life)' statement —
    no self-harm, overdose or risk wording anywhere."""
    t = text or ""
    if _STUCK_UNSAFE_RE.search(t):
        return False
    return any(rx.search(t) for rx in _STUCK_RES)


# "I just want to disappear" (Tab r13 item 3): vanish /
# disappear / done-with-everything, with NO life-or-death word
# anywhere in the message, is overwhelm — not a crisis
# statement. The keyword layer carries "(wish|want) … disappear"
# and "disappear forever" as crisis patterns, so the wish alone
# latched the full suicide reply on device. By rule now, like
# the med and stuck guards: the vanish family is cleared ONLY
# when it is the message's sole crisis-shaped signal — any
# other crisis pattern, any life-or-death word, or a finality
# marker (forever / for good / never come back) keeps the
# crisis routing untouched.
_VANISH_RES = [
    re.compile(r"\b(?:want|wish|wanna|need)\b[^.!?\n]{0,30}?"
               r"\b(?:disappear|vanish)\b", re.I),
    re.compile(r"\bjust\s+(?:want|wish|need)\s+to\s+"
               r"(?:disappear|vanish)\b", re.I),
    re.compile(r"\bi\s+want\s+to\s+(?:disappear|vanish)\b", re.I),
    re.compile(r"\b(?:i'?m|i\s+am)\s+done\s+with\s+everything\b",
               re.I),
    re.compile(r"\bdone\s+with\s+everything\b", re.I),
    re.compile(r"בא\s+לי\s+להיעלם"),
    re.compile(r"רוצה\s+להיעלם"),
    re.compile(r"נמאס\s+לי\s+מהכול"),
]
_VANISH_FINALITY_RE = re.compile(
    r"\bforever\b|\bfor\s+good\b|\bnever\s+(?:come|coming)\s+back\b|"
    r"\bpermanently\b|לתמיד|ולא\s+לחזור", re.I)
_VANISH_DEATH_RE = re.compile(
    r"\b(?:die|dead|death|kill|suicid\w*|self[-\s]?harm|hurt\s+"
    r"myself|harm\s+myself|end\s+(?:it|my\s+life)|never\s+wake|"
    r"not\s+wake\s+up)\b|"
    r"למות|להתאבד|להרוג|לפגוע\s+בעצמ|לסיים\s+(?:את\s+החיים|עם\s+החיים)",
    re.I)


def is_vanish_only_statement(text):
    """True when the message is a vanish / disappear /
    done-with-everything statement and NOTHING else in it is
    crisis-shaped: no other keyword pattern accounts for the
    words, no life-or-death word, no finality marker."""
    t = text or ""
    n = _norm_match(t)
    if not any(rx.search(n) for rx in _VANISH_RES) \
            and not any(rx.search(t) for rx in _VANISH_RES):
        return False
    if _VANISH_FINALITY_RE.search(n) or _VANISH_DEATH_RE.search(n):
        return False
    # Strip the vanish matches; if any crisis keyword pattern
    # still fires on what remains, the vanish words were not
    # the message's only signal.
    rest = n
    for rx in _VANISH_RES:
        rest = rx.sub(" ", rest)
    if detect_crisis_keywords(rest):
        return False
    return True


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
    # Licensed / credential questions (Tab round 5): "Are you
    # a licensed professional?" fell through to the model —
    # the professional patterns above demand a role noun
    # (therapist / psychologist / …), so "licensed
    # professional" never matched and the model answered with
    # "drawing on approaches like CBT, ACT". Same plain "No",
    # same answer strings ("professional" kind).
    ("professional", re.compile(
        r"^\s*are\s+you\s+(?:a\s+|an\s+)?"
        r"(?:licensed|certified|credentialed|qualified)\b",
        re.I)),
    ("professional", re.compile(
        r"^\s*(?:what\s+(?:are|is)\s+your|tell\s+me\s+your)\s+"
        r"(?:credentials|qualifications|licen[cs]e|"
        r"certification)\b", re.I)),
    ("professional", re.compile(
        r"^\s*do\s+you\s+have\s+(?:a\s+)?licen[cs]e\b", re.I)),
    ("professional", re.compile(
        r"^\s*(?:האם\s+)?(?:אתה|את)\s+"
        r"(?:איש\s+מקצוע\s+)?מורשה")),
    ("professional", re.compile(
        r"^\s*(?:האם\s+)?יש\s+לך\s+רישיון")),
    ("professional", re.compile(
        r"^\s*(?:האם\s+)?(?:אתה|את)\s+מוסמכ")),
    ("professional", re.compile(
        r"מה\s+(?:ה)?(?:הכשרה|הסמכה)\s+שלך")),
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
    # Diagnosis requests (Tab round 4): "Can you diagnose me?"
    # is the same family — a plain deterministic "No", this time
    # PLUS the referral line, like a real suggestion. Zero model
    # calls, so no dangling "What I can do…" can follow either.
    ("diagnose", re.compile(
        r"^\s*(?:can|could|will|would)\s+you\s+(?:please\s+)?"
        r"diagnose\s+me\b", re.I)),
    ("diagnose", re.compile(r"\bdiagnose\s+me\b", re.I)),
    ("diagnose", re.compile(
        r"^\s*do\s+i\s+have\s+(?!to\b)[^.!?\n]{0,60}[.!?]?$",
        re.I)),
    # A8 DIAGNOSE: any "do I have X / am I X / is this X / יש לי X"
    # shape — the label set names the form explicitly.
    ("diagnose", re.compile(
        r"^\s*am\s+i\s+(?:bipolar|depressed|autistic|schizophrenic|"
        r"mentally\s+ill|adhd|ocd|ptsd|borderline|narcissistic|"
        r"anorexic|bulimic|insomniac)\b", re.I)),
    ("diagnose", re.compile(
        r"^\s*is\s+this\s+(?:adhd|ocd|ptsd|depression|anxiety|"
        r"bipolar|autism|a\s+panic\s+attack)\b", re.I)),
    ("diagnose", re.compile(
        r"(?:אתה|את)\s+יכול(?:ה)?\s+לאבחן\s+אותי")),
    ("diagnose", re.compile(r"תאבחן\s+אותי")),
    ("diagnose", re.compile(r"לאבחן\s+אותי")),
    ("diagnose", re.compile(
        r"(?:האם\s+)?יש\s+לי\s+(?:דיכאון|חרדה|הפרעה|אוטיזם|"
        r"דו\s*קוטביות|אבחון)\s*\??")),
    ("diagnose", re.compile(
        r"(?:האם\s+)?אני\s+(?:דיכאוני|דיכאונית|דו\s*קוטבי|"
        r"דו\s*קוטבית|אוטיסט|אוטיסטית)\s*\??")),
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
    ("diagnose", False): (
        "No. I can't tell you whether you have a condition or "
        "what it might be — I'm Solace, an AI companion for "
        "reflection and self-help. For a question like that, the "
        "right person to talk to is a doctor or a licensed "
        "mental health professional."
    ),
    ("diagnose", True): (
        "לא. אני לא יכול לומר אם יש לך מצב כלשהו או מה הוא — "
        "אני סולאס, מלווה בינה מלאכותית להרהור ולתמיכה עצמית. "
        "לשאלה כזאת, הכתובת הנכונה היא רופא או איש מקצוע "
        "מורשה בתחום בריאות הנפש."
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
    # A12 evidence shape: "אפשר לכתוב לך באמצע הלילה?" — writing
    # in the middle of the night is the availability question.
    re.compile(
        r"אפשר\s+(?:לכתוב|לדבר|לשלוח)[^.!?\n]{0,20}?"
        r"(?:באמצע\s+הלילה|בלילה|בכל\s+שעה|מתי\s+ש)"),
    re.compile(
        r"\bcan\s+i\s+(?:write|message|text|talk|reach)\b"
        r"[^.!?\n]{0,30}?\b(?:middle\s+of\s+the\s+night|at\s+night|"
        r"any\s?time|whenever)\b", re.I),
]

AVAILABILITY_ANSWER = (
    "I'm here whenever you open Solace — what's on your mind?"
)
AVAILABILITY_ANSWER_HE = (
    "אני כאן כשתפתח את סולאס — מה עובר עליך?"
)

# Voice questions (Tab round 5): "Can I talk to you with my
# voice?" got a model answer claiming Solace is "text-only,
# no audio" — wrong: the app HAS Voice Mode. Like the identity
# and availability questions, this is answered deterministically,
# and the reply carries the approved voice-privacy line
# verbatim (EN + HE — the HE line is Tab's approved wording,
# masculine verbs, round-4 follow-up commit 60639cc).
_VOICE_Q_RES = [
    re.compile(
        r"\bdo\s+you\s+have\s+(?:a\s+)?voice\s+"
        r"(?:mode|chat|option|feature)?\b", re.I),
    re.compile(
        r"\bcan\s+i\s+(?:talk|speak|chat)\s+(?:to\s+you\s+)?"
        r"(?:with|using|by)\s+(?:my\s+)?voice\b", re.I),
    re.compile(r"\bcan\s+i\s+talk\s+to\s+you\s+out\s+loud\b",
               re.I),
    re.compile(r"\bcan\s+you\s+hear\s+me\b", re.I),
    # The capability family (Tab round 6): round 5 only knew
    # "voice mode" / "talk with my voice" — "Can you hear
    # voice messages?", "Can you hear my voice recordings?"
    # still got "I can only read text" (10/10). Hearing /
    # listening / understanding + a voice/audio object is
    # the same question and gets the same answer.
    re.compile(
        r"\bcan\s+you\s+(?:hear|listen\s+to|understand|play|"
        r"receive|get)\b[^.!?\n]{0,30}?\b(?:voice|audio|"
        r"recording)", re.I),
    re.compile(
        r"\bdo\s+you\s+(?:understand|accept|support|get)\s+"
        r"(?:voice|audio)\b", re.I),
    re.compile(
        r"\bvoice\s+(?:messages?|notes?|recordings?|memos?)\b"
        r"[^.!?\n]{0,25}\?", re.I),
    re.compile(r"\bspeak\s+instead\s+of\s+typ", re.I),
    re.compile(
        r"\bvoice\s+mode\b[^.!?\n]{0,20}\?", re.I),
    # A8 VOICE: speak / read aloud / store-or-keep audio shapes.
    re.compile(
        r"\bcan\s+you\s+(?:speak|read|say)\b[^.!?\n]{0,30}?"
        r"\b(?:answers|replies|out\s+loud|aloud|to\s+me)\b", re.I),
    re.compile(
        r"\bdo\s+you\s+(?:store|save|keep|record)\s+(?:my\s+)?"
        r"(?:audio|voice|recordings?)\b", re.I),
    re.compile(
        r"\bdo\s+you\s+record\s+(?:me|my|the)\b", re.I),
    re.compile(r"אפשר\s+לדבר\s+(?:איתך|אתך)\s+בקול"),
    re.compile(r"לדבר\s+(?:איתך|אתך)\s+בקול"),
    re.compile(r"יש\s+(?:לך\s+)?מצב\s+קול"),
    re.compile(r"(?:אתה|את)\s+יכול(?:ה)?\s+לשמוע\s+אותי"),
    re.compile(
        r"(?:אתה|את)\s+(?:שומר|מקליט)\s+את\s+(?:ה)?"
        r"(?:הקלטות|אודיו|קול)"),
    re.compile(r"(?:שומר|מקליט)\s+את\s+ההקלטות\s+שלי"),
    # Hebrew capability family (Tab round 6): "אתה יכול
    # לשמוע הודעות קוליות?" — לשמוע + a voice object, with
    # יכול/אפשר/מסוגל in front, in either order.
    re.compile(
        r"(?:יכול(?:ה)?|אפשר|מסוגל(?:ת)?)[^.!?\n]{0,15}?"
        r"לשמוע[^.!?\n]{0,20}?"
        r"(?:הודעות|הקלטות|קול|אודיו|הודעה|אותי)"),
    re.compile(
        r"לשמוע\s+(?:את\s+)?"
        r"(?:ההודעות|הקלטות|הודעות)\s+הקוליות"),
    re.compile(r"אפשר\s+לדבר\s+בקול"),
]
# Tab r13 item 4: questions about STORING / RECORDING the
# person's audio are a DIFFERENT label from "does voice exist"
# — the voice patterns above even carry the storage shapes, so
# "Do you store my audio?" got the voice answer, whose first
# word is "Yes". The truthful first word is No: the browser
# turns speech into text on the device, and Solace never
# receives or stores audio at all. This label is checked
# BEFORE the voice patterns in detect_intent_label.
_VOICE_STORAGE_RES = [
    re.compile(r"\b(?:store|save|keep|record|retain|upload)\b"
               r"[^.!?\n]{0,40}?\b(?:audio|voice|recordings?|"
               r"mic|microphone)\b", re.I),
    re.compile(r"\b(?:audio|voice|recordings?)\b[^.!?\n]{0,30}?"
               r"\b(?:stored|saved|kept|recorded|retained)\b",
               re.I),
    re.compile(r"\bdo\s+you\s+record\s+(?:me|us)\b", re.I),
    # Tab r14 item 6: the bare recording question — "are you
    # recording me?" / "אתה מקליט אותי?" carries no audio noun
    # for the patterns above to bite on, so it fell through to
    # the model. It is the same storage question and takes the
    # same No / לא answer.
    re.compile(r"\bare\s+you\s+recording\b", re.I),
    re.compile(r"\brecording\s+(?:me|us|this)\b", re.I),
    re.compile(r"מקליט(?:ה|ים|ות)?\s+אותי"),
    re.compile(r"(?:אתה|את)\s+מקליט(?:ה)?(?:\s|$)"),
    re.compile(r"(?:שומר|שומרת|שומרים|מקליט|מקליטה|מקליטים)"
               r"[^.!?\n]{0,30}?(?:קול|אודיו|הקלט|מיקרופון)"),
    re.compile(r"(?:הקלטות|האודיו|הקול)[^.!?\n]{0,20}?"
               r"(?:נשמר|נשמרות|נשמרים|מוקלט)"),
]

# Tab r14 item 6: a yes/no-shaped DATA question — "Do you sell
# my data?" — is a privacy question whose truthful first word
# is "No" / "לא". The generic privacy label's answer opens
# with the approved sentence itself, so these shapes get their
# own label whose answer is the No PLUS that same approved
# sentence, verbatim, after it.
_PRIVACY_YESNO_RES = [
    re.compile(r"\b(?:do|will|would)\s+you\s+(?:sell|share)\s+"
               r"(?:my|our)\s+(?:data|info(?:rmation)?|chats?|"
               r"messages?|conversations?|personal\s+\w+)\b", re.I),
    re.compile(r"\bare\s+you\s+(?:selling|sharing)\s+(?:my|our)\b",
               re.I),
    re.compile(r"\b(?:is|are)\s+(?:my|our)\s+(?:data|info|"
               r"information)\s+(?:being\s+)?(?:sold|shared)\b",
               re.I),
    re.compile(r"מוכר(?:ים|ות|ת)?\s+את\s+(?:ה)?(?:מידע|נתונים|"
               r"שיחות|הודעות)\s+של(?:י|נו)"),
    re.compile(r"(?:האם\s+)?(?:אתה|את|אתם)\s+[^.!?\n]{0,20}?"
               r"מוכר(?:ים|ות|ת)?\s+את\s+(?:ה)?(?:מידע|נתונים)"),
]

# A bare mention of voice messages / recordings counts as
# the voice question only in a question-shaped message, so
# "I left you a voice message" stays an ordinary message.
_VOICE_MENTION_RE = re.compile(
    r"(?:הודעות|הקלטות)\s+קוליות|"
    r"\bvoice\s+(?:messages?|recordings?|notes?)\b", re.I)

VOICE_ANSWER = (
    "Yes — Solace has Voice Mode: tap the 🎤 and you can talk "
    "out loud instead of typing. Solace never receives or "
    "stores your audio — only the text of what you say."
)
VOICE_ANSWER_HE = (
    "כן — יש לסולאס מצב קול: מקישים על ה־🎤 ואפשר לדבר בקול "
    "במקום להקליד. סולאס אף פעם לא מקבל או שומר את האודיו "
    "שלך — רק את הטקסט של מה שאתה אומר."
)
# Tab r13 item 4: the storage answer. First word "No" — the
# question is whether audio is stored, and the truthful answer
# is that it never reaches Solace at all. The Voice Mode offer
# follows as its own sentence. The Hebrew form keeps the
# writer-addressed tail neutral ("מה שנאמר") for an unknown
# gender (item 5); Solace's own verbs stay masculine, per A6.
VOICE_STORAGE_ANSWER = (
    "No. Solace never receives or stores your audio — only the "
    "text of what you say. The speech-to-text happens in your "
    "browser, on your device. If you'd like to talk instead of "
    "typing, tap the 🎤 for Voice Mode."
)
VOICE_STORAGE_ANSWER_HE = (
    "לא. סולאס אף פעם לא מקבל או שומר את האודיו שלך — רק את "
    "הטקסט של מה שנאמר. ההמרה מדיבור לטקסט קורית בדפדפן שלך, "
    "על המכשיר שלך. אם נוח לך יותר לדבר במקום להקליד, יש מצב "
    "קול — מקישים על ה־🎤."
)


def detect_voice_question(text):
    """True when the message asks whether voice can be used
    with Solace (Voice Mode exists — the deterministic answer
    says so; the model must never claim text-only)."""
    t = text or ""
    if any(rx.search(t) for rx in _VOICE_Q_RES):
        return True
    if _VOICE_MENTION_RE.search(t):
        return "?" in t or bool(
            re.match(r"\s*(?:can|do|does|is|are|האם|אפשר|אתה|את)\b",
                     t, re.I))
    return False


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
    # A8 SEEKING-HELP: "should I see someone about my anxiety?"
    re.compile(r"\bshould\s+i\s+see\s+someone\b", re.I),
    re.compile(r"\bdo\s+i\s+need\s+(?:to\s+see\s+)?someone\b",
               re.I),
    # HE: "אני צריכה ללכת לטיפול?" — need/must + (go to) therapy.
    re.compile(
        r"(?:צריך|צריכה|כדאי|חייב|חייבת)[^.!?\n]{0,20}?"
        r"(?:ללכת\s+)?(?:לטיפול|טיפול|לפסיכולוג|למטפל)"),
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


# A referral phrase inside a "can't tell you / can't decide"
# sentence is NOT a suggestion (Tab round 4): "אני לא יכול
# להגיד לך אם אתה צריך רופא או איש מקצוע…" mentions the words
# while refusing to suggest anything. Such an occurrence does
# not count as the referral being present.
_REFERRAL_NEGATED_RES = [
    re.compile(
        r"(לא\s+יכול(?:ה)?\s+(?:להגיד|לקבוע|לדעת|לומר|להחליט)|"
        r"אין\s+לי\s+דרך\s+(?:לדעת|לקבוע)|"
        r"לא\s+בסמכותי)"),
    re.compile(
        r"\b(?:can'?t|cannot|couldn'?t)\s+"
        r"(?:tell\s+you|decide|say|determine)\b", re.I),
    re.compile(r"\bnot\s+(?:able|the\s+one)\s+to\s+"
               r"(?:tell\s+you|decide|say)\b", re.I),
]


def _referral_present(reply, hebrew):
    """True when the approved referral phrase appears as a real
    suggestion — an occurrence inside a negated / can't-decide
    clause does not count."""
    phrase = REFERRAL_PHRASE_HE if hebrew else REFERRAL_PHRASE_EN
    hay = reply if hebrew else reply.lower()
    needle = phrase if hebrew else phrase.lower()
    if needle not in hay:
        return False
    for seg, _sep in _sentence_segments(reply):
        s = seg if hebrew else seg.lower()
        if needle in s and not any(
                rx.search(seg) for rx in _REFERRAL_NEGATED_RES):
            return True
    return False


def _ensure_referral(reply, hebrew, gender=None):
    """Guarantee the approved referral line is present (in the
    message's language) without disturbing anything else."""
    if _referral_present(reply, hebrew):
        return reply
    return reply + "\n\n" + _referral_append(hebrew, gender)


# ---- Tab FINAL order A10: on the labeled paths the referral line
# is added BY CODE, as its own separate sentence, exactly once.
# Any model sentence that refers to a professional is removed
# WHOLE and replaced by the line — code never splices words into
# a sentence (the "לפנות ל רופא" bug class).
REFERRAL_APPEND_HE_F = (
    "אם תרצי תמיכה מבחוץ, אפשר לדבר על זה עם "
    "רופא או איש מקצוע מורשה בתחום בריאות הנפש."
)
REFERRAL_APPEND_HE_N = (
    "אם מתאים, אפשר לדבר על זה עם "
    "רופא או איש מקצוע מורשה בתחום בריאות הנפש."
)


def _referral_append(hebrew, gender=None):
    if not hebrew:
        return REFERRAL_APPEND_EN
    if gender == "f":
        return REFERRAL_APPEND_HE_F
    if gender == "n":
        return REFERRAL_APPEND_HE_N
    # 'm', and the legacy default for callers that don't track
    # gender (the masculine form is the long-standing pin).
    return REFERRAL_APPEND_HE


_PROF_SENTENCE_RE = re.compile(
    r"(therapist|psychologist|counsel|psychiatr|licensed\s+"
    r"professional|mental\s+health\s+professional|"
    r"\ba\s+professional\b|רופא|מטפל|מטפלת|פסיכולוג|מוסמך|"
    r"איש\s+מקצוע|אשת\s+מקצוע)", re.I)


def _append_referral_once(reply, hebrew, gender=None):
    """A10: strip every sentence that refers to a professional,
    then append the approved referral line as its own sentence,
    exactly once, at the end."""
    segs = _sentence_segments(reply or "")
    kept = [(s, sep) for s, sep in segs
            if not _PROF_SENTENCE_RE.search(s)]
    base = "".join(s + sep for s, sep in kept).strip()
    line = _referral_append(hebrew, gender)
    return (base + "\n\n" + line) if base else line


# ---- Tab FINAL order A8/A9: ONE intent classification for every
# message; each label has ONE approved deterministic answer.
# Deterministic patterns first (below + the identity / voice /
# availability detectors), then the model classifier's intent.
_MEDICATION_Q_RES = [
    re.compile(r"\b(?:should|can|may)\s+i\s+(?:take|stop|start|"
               r"increase|decrease|change|skip|double|switch|"
               r"combine)\b[^.!?\n]{0,40}?"
               r"\b(?:medication|medicine|pill|dose|mg|prozac|zoloft|"
               r"lexapro|xanax|ritalin|adderall|ssri|antidepressant|"
               r"melatonin|sertraline|supplement)", re.I),
    re.compile(r"\bhow\s+(?:many|much)\b[^.!?\n]{0,30}?"
               r"\b(?:mg|pills?|tablets?|dose|melatonin)\b", re.I),
    re.compile(r"\bhow\s+much\b[^.!?\n]{0,25}?\bshould\s+i\s+take\b",
               re.I),
    re.compile(r"\bwhat\s+dose\b|\bdosage\b|\bdouble\s+my\s+dose\b",
               re.I),
    re.compile(r"\bcan\s+i\s+stop\s+(?:taking|my)\b", re.I),
    re.compile(r"\bshould\s+i\s+(?:take|stop|start|double|switch)\b"
               r"[^.!?\n]{0,30}?\b(?:it|them|my\s+\w+|melatonin|"
               r"sertraline|dose|pills?|medication)\b", re.I),
    re.compile(r"(?:להפסיק|לעצור|להעלות|להוריד|לשנות)\s+"
               r"(?:את\s+)?(?:התרופה|המינון|כדורים)"),
    re.compile(r"כמה\s+(?:מ\"ג|מ״ג|כדורים|מיליגרם|מלטונין)"),
    re.compile(r"(?:מינון|תרופה)[^.!?\n]{0,20}?"
               r"(?:להפסיק|להעלות|להוריד|לשנות|לקחת)"),
    re.compile(r"(?:אפשר|יכול(?:ה)?|כדאי)\s+(?:לי\s+)?"
               r"(?:להפסיק|להעלות|להוריד|לשנות|לקחת)\s+"
               r"(?:את\s+)?(?:הכדורים|התרופה|המינון)"),
]

MEDICATION_ANSWER = (
    "No — I can't advise on medication: not on doses, not on "
    "starting or stopping, and not on whether a medication is "
    "right for you. Decisions like that belong with a doctor who "
    "knows you."
)
MEDICATION_ANSWER_HE = (
    "לא — אני לא יכול לייעץ בנוגע לתרופות: לא על מינונים, לא על "
    "התחלה או הפסקה של תרופה, ולא אם תרופה מתאימה. החלטות כאלה "
    "שייכות לרופא שמכיר את המצב."
)

SEEKING_ANSWER = (
    "Reaching out for support is a good step, and I'm glad you're "
    "thinking about it. I can listen here and help you think "
    "things through — and for anything serious or ongoing, a "
    "person who knows your situation can help in ways I can't."
)
SEEKING_ANSWER_HE = (
    "לפנות לתמיכה זה צעד טוב, ואני שמח שזה עולה. אני יכול להקשיב "
    "כאן ולעזור לחשוב על הדברים — ובכל דבר רציני או מתמשך, אדם "
    "שמכיר את המצב יכול לעזור בדרכים שאני לא יכול."
)

HUMAN_ANSWER = (
    "No — I'm not a person. I'm Solace, an AI companion for "
    "reflection and self-help."
)
HUMAN_ANSWER_HE = (
    "לא — אני לא בן אדם. אני סולאס, מלווה בינה מלאכותית להרהור "
    "ולתמיכה עצמית."
)

# The ONE approved privacy sentence (kept exactly as approved);
# the referral line joins it by code, per A10.
PRIVACY_ANSWER = (
    "Your chats aren't shared with other people or used to "
    "advertise to you. See the privacy page for details."
)
PRIVACY_ANSWER_HE = (
    "השיחות שלך לא משותפות עם אנשים אחרים ולא משמשות כדי לפרסם "
    "לך. לפרטים נוספים, ראו את עמוד הפרטיות."
)

# A12: the approved availability line — the order quotes it
# verbatim: "I'm here whenever you open Solace…" (HE in m/f/n).
# It makes no 24/7 / any-time claim; it simply returns to the
# conversation.
_AVAILABILITY_HE = {
    "m": "אני כאן כשתפתח את סולאס — מה עובר עליך?",
    "f": "אני כאן כשתפתחי את סולאס — מה עובר עלייך?",
    "n": "אני כאן כשפותחים את סולאס — מה חדש?",
}

# Labels whose approved answer carries the referral line (A10 —
# the order's exact list: DIAGNOSE, MEDICATION, THERAPIST /
# REPLACE, SEEKING-HELP; on every path).
_REFERRAL_LABELS = {
    "therapist", "replace", "diagnose", "medication", "seeking",
}


def detect_medication_question(text):
    n = _norm_match(text)
    return any(rx.search(n) for rx in _MEDICATION_Q_RES)


_HUMAN_Q_RES = [
    re.compile(r"\bare\s+you\s+(?:a\s+)?(?:real\s+)?"
               r"(?:person|human)\b", re.I),
    re.compile(r"\bare\s+you\s+real\b", re.I),
    re.compile(r"\bam\s+i\s+talking\s+to\s+(?:a\s+)?"
               r"(?:real\s+)?(?:person|human|robot|bot|machine)\b",
               re.I),
    re.compile(r"\bwho\s+am\s+i\s+(?:really\s+)?talking\s+to\b",
               re.I),
    re.compile(r"(?:אתה|את)\s+(?:בן\s+אדם|אדם\s+אמיתי|"
               r"איש\s+אמיתי)\s*\??"),
    re.compile(r"(?:עם\s+מי|מול\s+מי)\s+אני\s+(?:מדבר|מדברת)"),
]

_PRIVACY_Q_RES = [
    re.compile(r"\bwho\s+(?:can|will|might)\s+(?:see|read)\s+"
               r"(?:this|my|our)\b", re.I),
    re.compile(r"\bis\s+this\s+(?:private|confidential|secret)\b",
               re.I),
    re.compile(r"\bprivacy\b[^.!?\n]{0,30}\?", re.I),
    re.compile(r"\bdo\s+you\s+(?:share|sell)\s+(?:my|our)\b", re.I),
    re.compile(r"\bcan\s+anyone\s+(?:see|read)\b", re.I),
    re.compile(r"\bare\s+my\s+(?:chats|messages|conversations)\s+"
               r"private\b", re.I),
    re.compile(r"מי\s+(?:יכול|עלול)\s+(?:לראות|לקרוא)"),
    re.compile(r"(?:האם|אם)\s+זה\s+(?:פרטי|חסוי)"),
    re.compile(r"מי\s+רואה\s+את\s+זה"),
    re.compile(r"(?:האם\s+)?(?:השיחות|ההודעות|השיחה)\s+שלי\s+"
               r"פרטי(?:ות|ת)?\s*\??"),
]


def _question_shaped(text):
    """Cheap gate for the model intent backstop: a question mark,
    a question-word opening, or a short message."""
    t = (text or "").strip()
    if "?" in t or "？" in t:
        return True
    if len(t.split()) <= 8:
        return True
    return bool(re.match(
        r"(?:are|can|could|do|does|is|should|would|will|what|who|"
        r"how|when|why|האם|אפשר|כמה|מה|מי|למה|מתי)\b", t, re.I))


# Unambiguous first names for gender inference (A5: Part B's own
# cases — a writer named מיכל is addressed as a woman, דני as a
# man — even before a first-person form appears). Unisex names
# are deliberately absent: no signal, neutral forms.
_NAME_GENDER_F = {
    "מיכל", "שרה", "רחל", "לאה", "נועה", "יעל", "תמר", "מרים",
    "חנה", "אסתר", "רבקה", "דבורה", "אביגיל", "הילה",
    "Michal", "Sarah", "Rachel", "Leah",
}
_NAME_GENDER_M = {
    "דני", "דוד", "משה", "אברהם", "יעקב", "יוסף", "אהרון",
    "שלמה", "יצחק", "אלון", "אבי", "יוסי", "חיים", "אלי",
    "Danny", "David", "Moshe", "Daniel",
}


def _gender_from_name(name):
    n = (name or "").strip()
    if not n:
        return None
    first = n.split()[0]
    if first in _NAME_GENDER_F:
        return "f"
    if first in _NAME_GENDER_M:
        return "m"
    return None


def detect_intent_label(text):
    """The deterministic half of the ONE intent classifier (A8):
    a fixed label or None. The model half runs in the same call
    as the risk classifier when these patterns don't fire."""
    t = text or ""
    identity_q = detect_identity_question(t)
    if identity_q == "therapy":
        # "Can you be my therapist?" is REPLACE; "Is this
        # therapy?" is THERAPIST — both take the plain No.
        if re.search(r"\bbe\s+my\b|\bbecome\s+my\b|להיות\s+"
                     r"(?:ה)?(?:מטפל|פסיכולוג)\s+שלי", t, re.I):
            return "replace"
        return "therapist"
    if identity_q == "professional":
        # The professional identity answer is the pin Tab's
        # earlier rounds set for this whole family ("Are you a
        # psychologist?" == IDENTITY_ANSWERS[professional],
        # test_tab_r9 item 4) and the FINAL order does not
        # redefine it — Part B line 59 asks only for a plain
        # "No…" / "לא…", which that answer gives.
        return "licensed"
    if identity_q == "diagnose":
        return "diagnose"
    if detect_medication_question(t):
        return "medication"
    if any(rx.search(t) for rx in _HUMAN_Q_RES):
        return "human"
    if any(rx.search(t) for rx in _PRIVACY_YESNO_RES):
        # r14 item 6: checked before the generic privacy
        # patterns — the yes/no shape needs its "No" first word.
        return "privacy_no"
    if any(rx.search(t) for rx in _PRIVACY_Q_RES):
        return "privacy"
    if any(rx.search(t) for rx in _VOICE_STORAGE_RES):
        # r13 item 4: storage questions are classified BEFORE
        # the voice capability patterns (which contain the same
        # store/record shapes) so the first word can be "No".
        return "voice_storage"
    if detect_voice_question(t):
        return "voice"
    if detect_availability_question(t):
        return "availability"
    if detect_need_professional(t):
        return "seeking"
    return None


def label_answer(label, hebrew, gender=None, name=None):
    """The ONE approved deterministic answer for a label (A8),
    with the referral line appended by code where A10 requires
    it. Returns None for an unknown label."""
    if label == "therapist":
        ans = IDENTITY_ANSWERS[("therapy", hebrew)]
        if hebrew:
            ans = _gendered_identity_he("therapy", gender)
    elif label == "replace":
        # A8's label is THERAPIST/REPLACE-THERAPIST, and Part B
        # #57 wants the answer to start "No." — the therapy
        # template ("No. Not therapy — ...") is that answer.
        ans = (_gendered_identity_he("therapy", gender) if hebrew
               else IDENTITY_ANSWERS[("therapy", False)])
    elif label == "licensed":
        ans = IDENTITY_ANSWERS[("professional", hebrew)]
        if hebrew:
            ans = _gendered_identity_he("licensed", gender)
    elif label == "diagnose":
        if hebrew:
            ans = _gendered_identity_he("diagnose", gender)
        else:
            # Referral-free form: the line is appended by code
            # below, exactly once (A10) — the IDENTITY_ANSWERS
            # original embeds its own referral sentence.
            ans = (
                "No. I can't tell you whether you have a "
                "condition or what it might be — I'm Solace, an "
                "AI companion for reflection and self-help."
            )
    elif label == "medication":
        ans = MEDICATION_ANSWER_HE if hebrew else MEDICATION_ANSWER
    elif label == "seeking":
        ans = SEEKING_ANSWER_HE if hebrew else SEEKING_ANSWER
    elif label == "human":
        ans = HUMAN_ANSWER_HE if hebrew else HUMAN_ANSWER
    elif label == "privacy":
        ans = PRIVACY_ANSWER_HE if hebrew else PRIVACY_ANSWER
    elif label == "privacy_no":
        # r14 item 6: the yes/no answer FIRST, then the one
        # approved privacy sentence, verbatim.
        ans = ("לא. " + PRIVACY_ANSWER_HE) if hebrew else (
            "No. " + PRIVACY_ANSWER)
    elif label == "voice_storage":
        ans = VOICE_STORAGE_ANSWER_HE if hebrew \
            else VOICE_STORAGE_ANSWER
    elif label == "voice":
        ans = VOICE_ANSWER_HE if hebrew else VOICE_ANSWER
        if hebrew:
            ans = _voice_answer_he(gender)
    elif label == "availability":
        ans = (_AVAILABILITY_HE.get(gender or "", _AVAILABILITY_HE["n"])
               if hebrew else AVAILABILITY_ANSWER)
    else:
        return None
    if label in _REFERRAL_LABELS:
        # A10: the referral line is added BY CODE as its own
        # sentence, exactly once. The label templates above are
        # referral-free by construction, so this is a plain
        # append — never a splice, never a strip.
        g = gender if gender in ("m", "f") else "n"
        ans = ans + "\n\n" + _referral_append(hebrew, g)
    return ans


# A9 output guard: a number + unit about a substance in a model
# reply means dosing content slipped through — the whole reply
# is replaced by the deterministic medication answer.
_DOSE_ADVICE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:mg|milligrams?|ml|grams?|tablets?|"
    r"pills?|capsules?|doses?)\b", re.I)
_SUBSTANCE_RE = re.compile(
    r"(medication|medicine|pill|tablet|dose|dosage|drug|prozac|"
    r"zoloft|lexapro|xanax|ritalin|adderall|antidepressant|ssri|"
    r"תרופ|כדור|מינון|מ\"ג|מ״ג)", re.I)


def has_dose_advice(reply):
    return bool(_DOSE_ADVICE_RE.search(reply or "")
                and _SUBSTANCE_RE.search(reply or ""))


# A12 output guard: a sentence claiming SOLACE is available any
# time / 24-7 / day or night never ships. Sentences about a
# hotline's hours are fine and stay.
_AVAIL_CLAIM_RE = re.compile(
    r"\b(?:i'?m|i\s+am|solace\s+is)\b[^.!?\n]{0,40}?"
    r"\b(?:24\s*/\s*7|any\s?time|always\s+(?:available|here|on)|"
    r"whenever\s+you\s+(?:need|want)|day\s+or\s+night|"
    r"around\s+the\s+clock)\b", re.I)
_AVAIL_CLAIM_HE_RE = re.compile(
    r"(?:אני|סולאס)[^.!?\n]{0,30}?"
    r"(?:זמין\s+תמיד|בכל\s+שעה|מסביב\s+לשעון|מתי\s+שתרצה|"
    r"זמינה\s+תמיד)")


def strip_availability_claims(reply):
    if not reply:
        return reply
    segs = _sentence_segments(reply)
    kept = []
    for s, sep in segs:
        if re.search(r"988|1201|741741|hotline|ער\"ן|ער״ן|קו\s", s):
            kept.append((s, sep))
            continue
        if _AVAIL_CLAIM_RE.search(s) or _AVAIL_CLAIM_HE_RE.search(s):
            continue
        kept.append((s, sep))
    if len(kept) == len(segs):
        return reply
    return "".join(s + sep for s, sep in kept).strip()


# ---- A5: gendered versions of the fixed Hebrew identity answers.
# Only the writer-addressed forms change; Solace's own forms stay
# masculine (A6). Where a version needs no gendered form, the
# neutral wording serves every gender.
_IDENTITY_HE_GENDERED = {
    "therapy": {
        "m": ("לא — זה לא טיפול. אני סולאס, מלווה בינה מלאכותית "
              "להרהור ולתמיכה עצמית, כאן כדי לדבר איתך על מה "
              "שעובר עליך."),
        "f": ("לא — זה לא טיפול. אני סולאס, מלווה בינה מלאכותית "
              "להרהור ולתמיכה עצמית, כאן כדי לדבר איתך על מה "
              "שעובר עלייך."),
        "n": ("לא — זה לא טיפול. אני סולאס, מלווה בינה מלאכותית "
              "להרהור ולתמיכה עצמית, כאן כדי לדבר על מה שעובר "
              "עכשיו."),
    },
    "replace": {
        "m": ("לא — אני לא יכול להיות המטפל שלך. אני סולאס, "
              "מלווה בינה מלאכותית להרהור ולתמיכה עצמית, ואני "
              "לא תופס מקום של איש מקצוע."),
        "f": ("לא — אני לא יכול להיות המטפל שלך. אני סולאס, "
              "מלווה בינה מלאכותית להרהור ולתמיכה עצמית, ואני "
              "לא תופס מקום של איש מקצוע."),
        "n": ("לא — אני לא יכול להיות המטפל שלך. אני סולאס, "
              "מלווה בינה מלאכותית להרהור ולתמיכה עצמית, ואני "
              "לא תופס מקום של איש מקצוע."),
    },
    "licensed": {
        "m": ("לא. אני סולאס — מלווה בינה מלאכותית להרהור "
              "ולתמיכה עצמית, לא איש מקצוע מורשה."),
        "f": ("לא. אני סולאס — מלווה בינה מלאכותית להרהור "
              "ולתמיכה עצמית, לא איש מקצוע מורשה."),
        "n": ("לא. אני סולאס — מלווה בינה מלאכותית להרהור "
              "ולתמיכה עצמית, לא איש מקצוע מורשה."),
    },
    "diagnose": {
        "m": ("לא. אני לא יכול לומר אם יש לך מצב כלשהו או מה "
              "הוא — אני סולאס, מלווה בינה מלאכותית להרהור "
              "ולתמיכה עצמית."),
        "f": ("לא. אני לא יכול לומר אם יש לך מצב כלשהו או מה "
              "הוא — אני סולאס, מלווה בינה מלאכותית להרהור "
              "ולתמיכה עצמית."),
        "n": ("לא. אני לא יכול לומר אם יש מצב כלשהו או מה הוא — "
              "אני סולאס, מלווה בינה מלאכותית להרהור ולתמיכה "
              "עצמית."),
    },
}


def _gendered_identity_he(kind, gender):
    versions = _IDENTITY_HE_GENDERED.get(kind)
    if not versions:
        return IDENTITY_ANSWERS[(kind, True)]
    return versions.get(gender or "", versions["n"])


def _voice_answer_he(gender):
    """A5: the Hebrew voice answer — the voice-privacy line's
    writer-addressed tail follows the writer's gender when it
    is known. Tab r13 item 5: an UNKNOWN gender is always
    neutral, in one consistent style — the tail becomes
    "של מה שנאמר" (no writer address at all; Solace's own
    verbs stay masculine per A6). A known masculine writer
    keeps the Tab-pinned string, byte-identical."""
    if gender == "f":
        return VOICE_ANSWER_HE.replace(
            "של מה שאתה אומר", "של מה שאת אומרת")
    if gender == "m":
        return VOICE_ANSWER_HE
    return VOICE_ANSWER_HE.replace(
        "של מה שאתה אומר", "של מה שנאמר")


# ---- A5 morphology: a paradigm of second-person present-tense
# pairs (masculine, feminine, infinitive). The post-generation
# check works off the PARADIGM, not phrase lists: any sentence
# whose second-person forms belong to the wrong gender is
# rewritten through the paradigm; for a writer of unknown gender
# the forms are neutralized through the infinitive frame.
_HE_VERB_PARADIGM = [
    # (masculine, feminine, infinitive)
    ("מרגיש", "מרגישה", "להרגיש"), ("צריך", "צריכה", "להצטרך"),
    ("יכול", "יכולה", "להיות מסוגל"), ("חושב", "חושבת", "לחשוב"),
    ("יודע", "יודעת", "לדעת"), ("נמצא", "נמצאת", "להימצא"),
    ("מסוגל", "מסוגלת", "להיות מסוגל"), ("אומר", "אומרת", "לומר"),
    ("שואל", "שואלת", "לשאול"), ("כותב", "כותבת", "לכתוב"),
    ("מדבר", "מדברת", "לדבר"), ("מבקש", "מבקשת", "לבקש"),
    ("מספר", "מספרת", "לספר"), ("רוצה", "רוצה", "לרצות"),
    ("עובר", "עוברת", "לעבור"), ("מבין", "מבינה", "להבין"),
    ("מרגיש", "מרגישה", "להרגיש"), ("שומע", "שומעת", "לשמוע"),
    ("רואה", "רואה", "לראות"), ("זוכר", "זוכרת", "לזכור"),
    ("מתמודד", "מתמודדת", "להתמודד"), ("חווה", "חווה", "לחוות"),
    ("נושם", "נושמת", "לנשום"), ("ישן", "ישנה", "לישון"),
    ("אוכל", "אוכלת", "לאכול"), ("שותה", "שותה", "לשתות"),
    ("עובד", "עובדת", "לעבוד"), ("לומד", "לומדת", "ללמוד"),
    ("מנסה", "מנסה", "לנסות"), ("מחפש", "מחפשת", "לחפש"),
    ("מקווה", "מקווה", "לקוות"), ("פוחד", "פוחדת", "לפחד"),
    ("דואג", "דואגת", "לדאוג"), ("מתגעגע", "מתגעגעת", "להתגעגע"),
    ("סובל", "סובלת", "לסבול"), ("בוכה", "בוכה", "לבכות"),
    ("צוחק", "צוחקת", "לצחוק"), ("קורא", "קוראת", "לקרוא"),
    ("כותב", "כותבת", "לכתוב"), ("שומר", "שומרת", "לשמור"),
]
_HE_M2F = {m: f for m, f, _i in _HE_VERB_PARADIGM if m != f}
_HE_F2M = {f: m for m, f, _i in _HE_VERB_PARADIGM if m != f}
_HE_M2INF = {m: i for m, f, i in _HE_VERB_PARADIGM}
_HE_F2INF = {f: i for m, f, i in _HE_VERB_PARADIGM}
# Plural forms — A5.2's sanctioned neutral: while the writer's
# gender is unknown, address can go plural (אתם + plural verb),
# which is grammatical, unlike a dropped pronoun.
_HE_PLURALS = {
    "מרגיש": "מרגישים", "מרגישה": "מרגישים", "צריך": "צריכים",
    "צריכה": "צריכים", "יכול": "יכולים", "יכולה": "יכולים",
    "חושב": "חושבים", "חושבת": "חושבים", "יודע": "יודעים",
    "יודעת": "יודעים", "נמצא": "נמצאים", "נמצאת": "נמצאים",
    "מסוגל": "מסוגלים", "מסוגלת": "מסוגלים", "אומר": "אומרים",
    "אומרת": "אומרים", "שואל": "שואלים", "שואלת": "שואלים",
    "כותב": "כותבים", "כותבת": "כותבים", "מדבר": "מדברים",
    "מדברת": "מדברים", "מבקש": "מבקשים", "מבקשת": "מבקשים",
    "מספר": "מספרים", "מספרת": "מספרים", "רוצה": "רוצים",
    "עובר": "עוברים", "עוברת": "עוברים", "מבין": "מבינים",
    "מבינה": "מבינים", "שומע": "שומעים", "שומעת": "שומעים",
    "רואה": "רואים", "זוכר": "זוכרים", "זוכרת": "זוכרים",
    "מתמודד": "מתמודדים", "מתמודדת": "מתמודדים",
    "חווה": "חווים", "נושם": "נושמים", "נושמת": "נושמים",
    "ישן": "ישנים", "ישנה": "ישנים", "אוכל": "אוכלים",
    "אוכלת": "אוכלים", "שותה": "שותים", "עובד": "עובדים",
    "עובדת": "עובדים", "לומד": "לומדים", "לומדת": "לומדים",
    "מנסה": "מנסים", "מחפש": "מחפשים", "מחפשת": "מחפשים",
    "מקווה": "מקווים", "פוחד": "פוחדים", "פוחדת": "פוחדים",
    "דואג": "דואגים", "דואגת": "דואגים", "מתגעגע": "מתגעגעים",
    "מתגעגעת": "מתגעגעים", "סובל": "סובלים", "סובלת": "סובלים",
    "בוכה": "בוכים", "בוכה": "בוכים", "צוחק": "צוחקים",
    "צוחקת": "צוחקים", "קורא": "קוראים", "קוראת": "קוראים",
    "שומר": "שומרים", "שומרת": "שומרים",
}
_HE_M_ALT = "|".join(sorted(_HE_M2F, key=len, reverse=True))
_HE_F_ALT = "|".join(sorted(_HE_F2M, key=len, reverse=True))
# Second-person frames: pronoun (+optional ש־) + paradigm verb.
_HE_ADDR_M_RE = re.compile(
    r"(שאתה|אתה)\s+(" + _HE_M_ALT + r")")
_HE_ADDR_F_RE = re.compile(
    r"(שאת|את)\s+(" + _HE_F_ALT + r")")
# Imperative pairs (m, f) beyond the paradigm.
_HE_IMP_PAIRS = [
    ("תקח", "תקחי"), ("תרגיש", "תרגישי"), ("תרצה", "תרצי"),
    ("תכתוב", "תכתבי"), ("תדבר", "תדברי"), ("תשאל", "תשאלי"),
    ("תבקש", "תבקשי"), ("תנסה", "תנסי"), ("תבוא", "תבואי"),
]
_HE_IMP_M2F = {m: f for m, f in _HE_IMP_PAIRS}
_HE_IMP_F2M = {f: m for m, f in _HE_IMP_PAIRS}
_HE_IMP_M_ALT = "|".join(_HE_IMP_M2F)
_HE_IMP_F_ALT = "|".join(_HE_IMP_F2M)


def _he_wrong_gender_markers(seg, target):
    """Morphological check (A5): unambiguous second-person forms
    of the OTHER gender in one sentence."""
    if target == "f":
        return bool(_HE_ADDR_M_RE.search(seg)
                    or re.search(r"(?<![\u0590-\u05FF])(?:"
                                 + _HE_IMP_M_ALT
                                 + r")(?![\u0590-\u05FF])", seg)
                    or re.search(r"שאתה\b", seg)
                    # pronoun suffixes (A5.4): עליך addressed to
                    # a woman is a wrong-gender marker.
                    or re.search(r"עליך(?![\u0590-\u05FF])", seg)
                    or re.search(r"(?<![\u0590-\u05FF])ספר\s+לי", seg))
    if target == "m":
        return bool(_HE_ADDR_F_RE.search(seg)
                    or re.search(r"(?<![\u0590-\u05FF])(?:"
                                 + _HE_IMP_F_ALT
                                 + r")(?![\u0590-\u05FF])", seg)
                    or re.search(r"שאת\b", seg)
                    or re.search(r"עלייך", seg)
                    or re.search(r"(?<![\u0590-\u05FF])ספרי\s+לי", seg))
    # neutral: a gendered second-person FRAME (pronoun + verb).
    # A bare pronoun inside a clause that is not an address
    # frame ("שמח שאתה כאן" — glad you are here) is not
    # rewritten: dropping it breaks the sentence, and the fixed
    # strings carry the neutral rule.
    # Tab r14 item 4: כשאתה / כשאת is a marker too — the frame
    # rewrite fixes it when a paradigm verb follows (שאתם +
    # plural), the kshe pass fixes the future forms, and any
    # other כשאתה sentence is unfixable and drops, so the form
    # never ships to an unknown writer.
    return bool(_HE_ADDR_M_RE.search(seg) or _HE_ADDR_F_RE.search(seg)
                or re.search(r"כשאתה(?![\u0590-\u05FF])", seg)
                or re.search(r"כשאת(?![\u0590-\u05FF])", seg))


def _he_rewrite_gender_sentence(seg, target):
    """Rewrite one sentence's second-person forms toward the
    target through the paradigm. For the neutral target the
    pronoun drops and the verb becomes its infinitive frame
    ('אפשר …'), so no אתה / את address remains."""
    if target == "f":
        out = _HE_ADDR_M_RE.sub(
            lambda m: ("שאת" if m.group(1) == "שאתה" else "את")
            + " " + _HE_M2F[m.group(2)], seg)
        out = re.sub(r"(?<![\u0590-\u05FF])(" + _HE_IMP_M_ALT
                     + r")(?![\u0590-\u05FF])",
                     lambda m: _HE_IMP_M2F[m.group(1)], out)
        # Bare pronouns outside the paradigm frames swap too —
        # אתה / שאתה are unambiguous address forms.
        out = re.sub(r"שאתה\b", "שאת", out)
        out = re.sub(r"(?<![\u0590-\u05FF])אתה\b", "את", out)
        # Pronoun suffix + the tell imperative (A5.4).
        out = re.sub(r"עליך(?![\u0590-\u05FF])", "עלייך", out)
        out = re.sub(r"(?<![\u0590-\u05FF])ספר\s+לי", "ספרי לי", out)
        return out
    if target == "m":
        out = _HE_ADDR_F_RE.sub(
            lambda m: ("שאתה" if m.group(1) == "שאת" else "אתה")
            + " " + _HE_F2M[m.group(2)], seg)
        out = re.sub(r"(?<![\u0590-\u05FF])(" + _HE_IMP_F_ALT
                     + r")(?![\u0590-\u05FF])",
                     lambda m: _HE_IMP_F2M[m.group(1)], out)
        out = re.sub(r"שאת\b", "שאתה", out)
        out = out.replace("עלייך", "עליך")
        out = re.sub(r"(?<![\u0590-\u05FF])ספרי\s+לי", "ספר לי", out)
        return out
    # neutral: pronoun+verb -> plural address (אתם + plural
    # verb), A5.2's sanctioned neutral — grammatical in every
    # frame, unlike a dropped pronoun or a forced infinitive.
    def _neut(m):
        plural = _HE_PLURALS.get(m.group(2))
        if not plural:
            return m.group(0)
        if m.group(1).startswith("ש"):
            return "שאתם " + plural
        return "אתם " + plural

    out = _HE_ADDR_M_RE.sub(_neut, seg)
    out = _HE_ADDR_F_RE.sub(_neut, out)
    return out


def he_gender_repair(text, gender):
    """The A5 post-generation morphology pass for chat replies:
    every sentence's second-person forms are checked against the
    writer's stored gender ('m' / 'f'), or neutralized to the
    plural while the gender is unknown ('n'). A None gender means
    the caller isn't tracking gender (legacy unit surfaces) and
    the text passes through. Sentences that still carry the
    wrong gender after the paradigm rewrite are dropped rather
    than shipped misgendered."""
    if not text or not has_hebrew(text):
        return text
    if gender is None:
        return text
    target = gender if gender in ("m", "f") else "n"
    # כש-prefixed futures ("כשתרצי") hide from the pronoun
    # frames — normalize them directly for the target first.
    _kshe = {"תרצה": ("תרצה", "תרצי", "תרצו"),
             "תרצי": ("תרצה", "תרצי", "תרצו"),
             "תרגיש": ("תרגיש", "תרגישי", "תרגישו"),
             "תרגישי": ("תרגיש", "תרגישי", "תרגישו"),
             "תוכל": ("תוכל", "תוכלי", "תוכלו"),
             "תוכלי": ("תוכל", "תוכלי", "תוכלו"),
             "תהיה": ("תהיה", "תהיי", "תהיו"),
             "תהיי": ("תהיה", "תהיי", "תהיו")}
    _ki = {"m": 0, "f": 1, "n": 2}[target]
    text = re.sub(
        r"כש(תרצה|תרצי|תרגיש|תרגישי|תוכל|תוכלי|תהיה|תהיי)"
        r"(?![\u0590-\u05FF])",
        lambda mm: "כש" + _kshe[mm.group(1)][_ki], text)
    if target == "n":
        # Bare imperatives carry gender too ("קחי את הזמן").
        # Tab r14 item 4 SUPERSEDES the old plural rule: the
        # plural minting produced exactly the banned forms —
        # ספרו (named by Tab) and דברו (banned outright by
        # item 5). An unknown writer's imperative now ships in
        # its plain (masculine) form, the round-6 scrub rule:
        # feminine imperatives resolve to the bare stem and
        # masculine ones pass through untouched.
        for fem, masc in (("קחי", "קח"), ("ספרי", "ספר"),
                          ("כתבי", "כתוב"), ("בואי", "בוא"),
                          ("שבי", "שב"), ("תני", "תן"),
                          ("דברי", "דבר"), ("הקשיבי", "הקשב")):
            text = re.sub(r"(?<![\u0590-\u05FF])" + fem
                          + r"(?![\u0590-\u05FF])", masc, text)
        # כש + pronoun + plural future ("כשאתה תרצו", after the
        # kshe pass above): the pronoun drops — "כשתרצו" is the
        # neutral frame, and כשאתה never ships (item 4).
        text = re.sub(
            r"כש(?:אתה|את)\s+(תרצו|תרגישו|תוכלו|תהיו)",
            r"כש\1", text)
    segs = _sentence_segments(text)
    out_segs = []
    changed = False
    for s, sep in segs:
        if s.strip() and _he_wrong_gender_markers(s, target):
            fixed = _he_rewrite_gender_sentence(s, target)
            changed = True
            if _he_wrong_gender_markers(fixed, target):
                continue  # unfixable — dropped, never shipped wrong
            out_segs.append((fixed, sep))
        else:
            out_segs.append((s, sep))
    if not changed:
        return text
    out = "".join(s + sep for s, sep in out_segs).strip()
    return out or text


# A15: no "/" between Hebrew letters ever ships. Any slash pair
# the named resolvers didn't know is resolved generically —
# feminine takes the second form, everyone else the first.
_HE_SLASH_ANY_RE = re.compile(
    r"([\u0590-\u05FF]+)/([\u0590-\u05FF]+)")


def _he_gender_vocab():
    vocab = getattr(_he_gender_vocab, "_set", None)
    if vocab is None:
        vocab = frozenset(
            [m for m, _f, _i in _HE_VERB_PARADIGM]
            + [f for _m, f, _i in _HE_VERB_PARADIGM]
            + list(_HE_SELF_FEM_MASC.keys())
            + list(_HE_SELF_FEM_MASC.values())
            + list(_HE_WRITER_FEM) + list(_HE_WRITER_MASC)
            + ["אתה", "את", "שאתה", "שאת", "בן", "בת", "איש",
               "אישה", "תרצה", "תרצי", "תרגיש", "תרגישי",
               "מסוגל", "מסוגלת", "קח", "קחי", "ספר", "ספרי",
               "כתוב", "כתבי", "בוא", "בואי", "שב", "שבי",
               "תן", "תני", "דבר", "דברי", "הקשב", "הקשיבי"])
        _he_gender_vocab._set = vocab
    return vocab


def strip_hebrew_slashes(text, writer_gender=None):
    """A15: no "/" between Hebrew letters in GENDER pairs —
    resolved by the writer's gender. A slash between two words
    that are not gender forms at all (יום/לילה) is not an
    address form and is left alone (Tab round 10 pin)."""
    if not text or "/" not in text:
        return text

    def _sub(m):
        if m.group(1) in _he_gender_vocab() \
                or m.group(2) in _he_gender_vocab():
            return m.group(2) if writer_gender == "f" else m.group(1)
        return m.group(0)

    return _HE_SLASH_ANY_RE.sub(_sub, text)


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
# Feminine-WRITER variants (Tab round 4): when the person
# writing is a woman ("אני מתגעגעת"), the fixed opening must
# address HER in feminine forms (קחי… שאת צריכה… כשתרצי) —
# round 3 forced everything to the masculine and misgendered
# her in every reply. Solace's own forms (אני ממש מצטער) stay
# masculine — that is Solace speaking about itself.
GRIEF_OPENING_HE_WF = (
    "אני ממש מצטער על האובדן שלך. לאבד מישהו שאוהבים משנה הכול, "
    "ואין דרך נכונה להתאבל. אני כאן איתך — קחי את כל הזמן שאת צריכה. "
    "כשתרצי, אשמח לשמוע עליו."
)
GRIEF_OPENING_HE_WF_F = (
    "אני ממש מצטער על האובדן שלך. לאבד מישהו שאוהבים משנה הכול, "
    "ואין דרך נכונה להתאבל. אני כאן איתך — קחי את כל הזמן שאת צריכה. "
    "כשתרצי, אשמח לשמוע עליה."
)
# Neutral-writer variants (Tab FINAL order A5): while the writer's
# gender is unknown, the opening addresses no gender at all —
# impersonal frames instead of קח / שאתה צריך / כשתרצה.
GRIEF_OPENING_HE_N = (
    "אני ממש מצטער על האובדן שלך. לאבד מישהו שאוהבים משנה הכול, "
    "ואין דרך נכונה להתאבל. אני כאן איתך — אפשר לקחת את כל הזמן "
    "שצריך. כשמתאים, אשמח לשמוע עליו."
)
GRIEF_OPENING_HE_N_F = (
    "אני ממש מצטער על האובדן שלך. לאבד מישהו שאוהבים משנה הכול, "
    "ואין דרך נכונה להתאבל. אני כאן איתך — אפשר לקחת את כל הזמן "
    "שצריך. כשמתאים, אשמח לשמוע עליה."
)
_GRIEF_KIN_HE_F = re.compile(
    r"(אמא|סבתא|אחותי|אחות\s+שלי|אשתי|הבת\s+שלי|בת\s+שלי|חברה\s+שלי|"
    r"הכלבה\s+שלי|כלבה\s+שלי|החתולה\s+שלי|חתולה\s+שלי)")
_GRIEF_KIN_HE_M = re.compile(
    r"(אבא|סבא|אחי|אח\s+שלי|בעלי|הבן\s+שלי|בן\s+שלי|חבר\s+שלי|"
    r"הכלב\s+שלי|כלב\s+שלי|החתול\s+שלי|חתול\s+שלי)")


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


# Writer-gender detection (Tab round 4): a Hebrew writer
# genders THEMSELVES in self-referential forms — מתגעגעת /
# צריכה / מרגישה (feminine) vs מתגעגע / צריך / מרגיש
# (masculine). The first such signal in the message wins;
# no signal -> None (the reply then follows the fixed
# opening's gender, as rounds 2-3 pinned). Word-edge lookarounds
# keep צריכה from matching inside צריך and the like.
_HE_WRITER_FEM = (
    "מתגעגעת", "צריכה", "מרגישה", "שמחה", "עייפה", "אלמנה",
    "יכולה", "חושבת", "יודעת", "מבקשת", "מספרת", "כותבת",
    "מדברת", "אומרת", "שואלת", "מסוגלת", "נמצאת", "מוכנה",
    "בטוחה", "עצובה", "מצטערת", "אוהבת", "שומעת",
    "מצליחה", "ממשיכה", "חוזרת", "הולכת", "גדלה",
)
_HE_WRITER_MASC = (
    "מתגעגע", "צריך", "מרגיש", "שמח", "עייף", "אלמן",
    "יכול", "חושב", "יודע", "מבקש", "מספר", "כותב",
    "מדבר", "אומר", "שואל", "מסוגל", "נמצא", "מוכן",
    "בטוח", "עצוב", "מצטער", "אוהב", "שומע",
    "מצליח", "ממשיך", "חוזר", "הולך", "גדל",
)
_HE_WRITER_GENDER_RE = re.compile(
    r"(?<![\u0590-\u05FF])(?:"
    + "|".join(_HE_WRITER_FEM + _HE_WRITER_MASC)
    + r")(?![\u0590-\u05FF])")


def detect_writer_gender_he(text):
    """'f' / 'm' / None — the writer's own gender, from the
    first self-referential gendered form in their message.
    The form must be FIRST-person anchored — it follows אני
    (closely, allowing one filler word: "אני לא יכולה",
    "אני ממש מרגישה", "שאני מתגעגעת"). A bare gendered verb
    can be SECOND person ("אתה יכול לאבחן אותי?" — "you can")
    and must never mark the writer (A5: the stored gender is
    authoritative, so a false read poisons every string)."""
    t = text or ""
    for m in _HE_WRITER_GENDER_RE.finditer(t):
        before = t[max(0, m.start() - 16):m.start()]
        toks = before.split()
        # Anchored when אני (or ואני / שאני) is the last token
        # or the one before a single filler word ("אני לא
        # יכולה", "אני ממש מרגישה").
        if toks and (toks[-1] in ("אני", "ואני", "שאני")
                     or (len(toks) >= 2
                         and toks[-2] in ("אני", "ואני", "שאני"))):
            return "f" if m.group(0) in _HE_WRITER_FEM else "m"
    return None


def grief_opening_for(hebrew, msg, writer_gender=None):
    """The exact fixed opening for this grief turn: Hebrew gets
    the gender-correct pronoun for the person who died, and —
    when the writer's gender is known — address forms in the
    WRITER's gender (Tab round 4)."""
    if not hebrew:
        return GRIEF_OPENING
    kin_f = detect_grief_kin_he(msg) == "f"
    if writer_gender == "f":
        return GRIEF_OPENING_HE_WF_F if kin_f else GRIEF_OPENING_HE_WF
    if writer_gender == "m":
        if kin_f:
            return GRIEF_OPENING_HE_F
        return GRIEF_OPENING_HE
    # A5: unknown writer gender -> the neutral opening.
    return GRIEF_OPENING_HE_N_F if kin_f else GRIEF_OPENING_HE_N
# Continuation used only if the model call fails on a grief turn.
GRIEF_FALLBACK = (
    "I'm here. There's nothing to figure out right now — it's "
    "okay to just sit with this for a while."
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
_GRIEF_KIN_NOTES = {
    "f": (
        "the person who died is a WOMAN. Refer to her ONLY in "
        "feminine forms (היא, עליה, הייתה, שלה) — never עליו, "
        "הוא, היה or ממנו about her."
    ),
    "m": (
        "the person who died is a MAN. Refer to him ONLY in "
        "masculine forms (הוא, עליו, היה, שלו) — never עליה, "
        "היא, הייתה or ממנה about him."
    ),
    None: (
        "keep ONE consistent gender for the person who died "
        "(the forms of the opening line govern)."
    ),
}

# Addressee instruction (Tab round 4): follows the WRITER's
# gender when the writer's own message revealed it; otherwise
# the opening line's forms govern, as before.
_GRIEF_ADDRESSEE_NOTES = {
    "f": (
        "Address the person you are speaking with in FEMININE "
        "singular, exactly as the opening line does (קחי את "
        "הזמן, שאת צריכה, כשתרצי)."
    ),
    "m": (
        "Address the person you are speaking with in "
        "masculine singular, exactly as the opening line does "
        "(קח את הזמן, כשתרצה)."
    ),
    None: (
        "Address the person you are speaking with exactly as "
        "the opening line does."
    ),
}


def _grief_gender_note(kin, writer_gender):
    """The Hebrew-grief grammar directive for one turn: the
    deceased's gender (kin), the addressee's gender (the
    writer's own, when known), one gender throughout, no slash
    forms, no separator lines, ONE continuous reply."""
    return (
        "\n\nHEBREW GRAMMAR — CRITICAL for this reply: "
        + _GRIEF_KIN_NOTES[kin] + " "
        + _GRIEF_ADDRESSEE_NOTES[writer_gender]
        + " Never use slash forms such as מקשיב/ה — choose "
        "the one matching form and stay with it. Never write "
        "a separator line such as ---: write ONE continuous "
        "reply that continues from the opening line. Address "
        "the person in ONE gender from start to finish; if a "
        "second-person form is uncertain, use a gender-neutral "
        "construction (an infinitive or a noun form) — never "
        "switch gender mid-reply."
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
- NO NAMED-APPROACH CLAIMS: never say you draw on, use, are trained in, or are based on named approaches or schools (CBT, ACT, DBT, psychotherapy and the like), and never imply a license or credentials. If asked what you are, answer plainly: an AI companion for reflection and self-help. (You may still quietly let good listening practice shape how you respond — just never advertise named approaches as your method or credentials.)
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
- NO INVENTED WORRIES: never open a reply by claiming you can see, sense, or already know that something is bothering, troubling, or weighing on the person when they haven't said so — no "I can see something is bothering you" openers to a plain greeting. Respond to what they actually wrote; ask, don't presume.

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
    # Round 5: a brief busy window instead of an instant
    # "database is locked" under simultaneous-start write
    # bursts; the journal is WAL (set in init_db), so readers
    # no longer queue behind a writer at all, and NORMAL
    # synchronous (the standard WAL pairing) keeps each
    # commit's fsync off the crisis path's critical queue.
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    with db_lock:
        conn = _conn()
        # WAL (Tab round 5): readers never block behind a
        # writer — one of the simultaneous-start stalls on the
        # crisis fast path. The mode persists in the DB file;
        # setting it on every boot is a no-op once active.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
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
        # Tab FINAL order (A5/A2): the writer's own gender, once
        # their Hebrew reveals it, is stored per user and steers
        # every user-addressed form from then on; crisis_checkin
        # records that the ONE gentle post-crisis check-in is owed.
        if "writer_gender" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN writer_gender TEXT DEFAULT ''"
            )
        if "crisis_checkin" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN crisis_checkin INTEGER DEFAULT 0"
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
# One shared HTTP client for API calls (Tab round 5):
# previously EVERY call_anthropic built a fresh httpx.Client,
# so at simultaneous start nine chats each paid a full TLS
# handshake for their classifier call and another for the
# chat call — pure contention on a throttled CPU. httpx
# Clients are thread-safe; connections now stay warm.
_http_client = None
_http_client_lock = threading.Lock()


def _get_http_client():
    global _http_client
    if _http_client is None:
        with _http_client_lock:
            if _http_client is None:
                _http_client = httpx.Client(timeout=120)
    return _http_client


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
        r = _get_http_client().post(
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
    """A4: the deterministic check runs on the message AND on its
    normalized form (niqqud / punctuation stripped, repeated
    letters collapsed, slang expanded, one-letter typos on the
    crisis keywords repaired) — a typo can no longer slip a
    crisis phrase past the fast path."""
    if _detect_crisis_keywords_raw(text):
        return True
    n = _norm_match(text)
    if n != (text or "").lower():
        return _detect_crisis_keywords_raw(n)
    return False


def _detect_crisis_keywords_raw(text):
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


INTENT_CLASSIFIER_SYSTEM = (
    "You classify one message to a support companion app. Reply "
    "with exactly one label: THERAPIST (asking if it is therapy "
    "or a therapist), REPLACE (asking it to be their therapist), "
    "LICENSED (asking about license/credentials), DIAGNOSE "
    "(asking for a diagnosis), MEDICATION (asking for medication "
    "advice, doses, starting or stopping), SEEKING (asking if "
    "they should get professional help), HUMAN (asking if it is "
    "a real person), PRIVACY (asking who can see the chat), "
    "VOICE (asking about voice/speaking), AVAILABILITY (asking "
    "if it is available 24/7 or when), or NONE."
)
_INTENT_LABEL_WORDS = {
    "THERAPIST": "therapist", "REPLACE": "replace",
    "LICENSED": "licensed", "DIAGNOSE": "diagnose",
    "MEDICATION": "medication", "SEEKING": "seeking",
    "HUMAN": "human", "PRIVACY": "privacy", "VOICE": "voice",
    "AVAILABILITY": "availability",
}


def classify_intent_model(text):
    """The model half of the ONE intent classifier (A8): runs on
    every message the deterministic patterns didn't label. Fails
    to None — a missed label falls back to the normal flow."""
    if os.environ.get("CRISIS_CLASSIFIER", "on").lower() == "off":
        return None
    try:
        out = call_anthropic(
            SUMMARY_MODEL,
            INTENT_CLASSIFIER_SYSTEM,
            [{"role": "user", "content": text}],
            max_tokens=6,
        )
        word = (out or "").strip().upper().split()[0].strip(".:")
        return _INTENT_LABEL_WORDS.get(word)
    except Exception:
        return None


def assess_risk(text):
    """Full pre-model risk check: keyword layer first, classifier second.
    Runs BEFORE any normal model call, on every user message."""
    if is_vanish_only_statement(text):
        # Tab r13 item 3: checked before the keyword layer —
        # the guard itself verified the vanish words are the
        # message's only crisis-shaped signal.
        return False
    if detect_crisis_keywords(text):
        return True
    if detect_clarification(text):
        # A walk-back is not-crisis BY RULE (Tab r13 follow-up):
        # the clarification detector's frames (joking / didn't
        # mean it / I'm safe / התכוונתי ש…) are deterministic,
        # and the live classifier was overriding them — a Hebrew
        # "I meant I'm dead TIRED" retraction after a latched
        # crisis got a model reply instead of the fixed clarify
        # line, because the classifier escalated it first. The
        # keyword layer above still wins for any message that
        # carries a real crisis phrase alongside the walk-back.
        return False
    if is_prescribed_med_statement(text):
        # Plain medication talk is not-crisis by rule (Tab round
        # 4) — the classifier never gets to escalate it.
        return False
    if is_stuck_in_life_statement(text):
        # "Feeling stuck in life" is not-crisis by rule (Tab
        # round 6) — the classifier never gets to escalate it.
        return False
    if is_vanish_only_statement(text):
        # Vanish / disappear / done-with-everything, with no
        # life-or-death word, is not-crisis by rule (Tab r13) —
        # checked before the classifier; its own guard already
        # verified no other crisis signal is present.
        return False
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
        r"אשתי|הבן שלי|הבת שלי|חבר שלי|חברה שלי|"
        r"הכלב שלי|הכלבה שלי|החתול שלי|החתולה שלי)"
        r"[^.!?\n]{0,25}?"
        r"(?:נפטר|נפטרה|נפטרו|מת|מתה|נהרג|נהרגה|הלך לעולמו|הלכה לעולמה)"
    ),
    re.compile(r"(?:נפטר|נפטרה)\s+(?:לי\s+)?(?:ה)?(?:אמא|אבא|סבתא|סבא)"),
    re.compile(
        r"איבדתי\s+את\s+(?:ה)?(?:אמא|אבא|סבתא|סבא|אחי|אחותי|בעלי|אשתי|"
        r"הכלב|הכלבה|החתול|החתולה)"
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


def _main_language(text):
    """'he' / 'en' / None — the MAIN language of one message, by
    letter counts (Tab r14 item 7). Decisive only when one
    script clearly dominates (>= 4 letters and strictly more
    than the other): an English message carrying one Hebrew
    phrase is an ENGLISH message — the pre-r14 rule (any Hebrew
    character at all makes it Hebrew) answered it in Hebrew.
    Short or balanced messages return None and the callers fall
    back to their older signals."""
    he = len(re.findall(r"[\u0590-\u05FF]", text or ""))
    lat = len(re.findall(r"[A-Za-z]", text or ""))
    if max(he, lat) < 4 or he == lat:
        return None
    return "he" if he > lat else "en"


def conversation_is_hebrew(hist_rows, msg):
    """True when the conversation is in Hebrew. Tab r14 item 7:
    the current message's MAIN language decides when it is
    decisive — an English message with one Hebrew phrase keeps
    the conversation English. An indecisive message (short, or
    typed in transliteration) falls back to the older signal:
    any Hebrew in it, or in any of the last few user messages —
    so a Hebrew conversation stays fully Hebrew through short
    follow-ups."""
    lang = _main_language(msg)
    if lang is not None:
        return lang == "he"
    if has_hebrew(msg):
        return True
    user_msgs = [r["content"] for r in hist_rows if r["role"] == "user"]
    return any(has_hebrew(c) for c in user_msgs[-3:])


def message_is_hebrew(msg):
    """Language of THIS message only. Crisis routing uses this —
    never conversation history — so an English crisis message right
    after a Hebrew one gets the English fixed reply, banner and 988,
    and vice versa (Tab round 3). Tab r14 item 7: the message's
    MAIN language — an English message with one Hebrew phrase
    is English here too; only an indecisive (tiny) message falls
    back to the any-Hebrew signal."""
    lang = _main_language(msg)
    if lang is not None:
        return lang == "he"
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
    # A13.3: never mention the intake, the form, the chips, or
    # what was or wasn't filled in — and never claim to know a
    # worry the person did not state in the conversation itself.
    parts.append(
        "Never mention any intake, form, questionnaire, chips or "
        "check-in questions, and never refer to what the person "
        "did or did not fill in. Everything you know about them "
        "comes from this conversation and the memory note above.")
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


# Approach claims (Tab round 5): "drawing on approaches like
# CBT, ACT" — a reply must never claim Solace draws on, uses,
# or is based on NAMED therapy approaches, and never imply
# licensure or credentials. The identity short-circuit answers
# the direct question deterministically; this sentence-level
# scrub is the second wall, for every other surface. A plain
# informational mention that carries no self-claim shape
# ("some people find CBT helpful") is not this pattern's
# target — the claim shapes are.
_APPROACH_CLAIM_RES = [
    re.compile(
        r"\bdraw(?:s|ing|n)?\s+on\b[^.!?\n]{0,60}?\b"
        r"(?:approaches?|techniques?|methods?|CBT|ACT|DBT)\b",
        re.I),
    re.compile(
        r"\bapproaches?\s+(?:like|such\s+as)\s+"
        r"(?:CBT|ACT|DBT)\b", re.I),
    re.compile(
        r"\b(?:i|we|solace)\s+(?:use|uses|using|apply|applies|"
        r"practice|practise|work\s+with)\b[^.!?\n]{0,60}?\b"
        r"(?:CBT|ACT|DBT)\b", re.I),
    re.compile(
        r"\btrained\s+in\s+(?:CBT|ACT|DBT|psychotherapy)\b",
        re.I),
    re.compile(
        r"\bbased\s+on\s+(?:evidence-based\s+)?"
        r"(?:CBT|ACT|DBT)\b", re.I),
    re.compile(
        r"\bmy\s+(?:training|background|approach)\s+in\s+"
        r"(?:CBT|ACT|DBT)\b", re.I),
    re.compile(r"גישות\s+כמו\s+(?:CBT|ACT|DBT)"),
    re.compile(
        r"(?:מבוסס|מבוססת|משתמש|משתמשת|עובד|עובדת)\s+"
        r"(?:ב|עם)?[^.!?\n]{0,25}?(?:CBT|ACT|DBT)"),
]


def _sentence_approach_claim(seg):
    """True when one sentence claims Solace draws on / uses /
    is based on named therapy approaches."""
    return any(rx.search(seg) for rx in _APPROACH_CLAIM_RES)


# Voice-denial claims (Tab round 5): the app HAS Voice Mode,
# so a model sentence claiming Solace is text-only or cannot
# receive audio is false and is removed like the other claim
# families. (A persona in Rehearse saying "I can't hear you"
# mid-scene is not this shape — these patterns are capability
# statements about the chat itself.)
_VOICE_DENIAL_RES = [
    re.compile(r"\btext[-\s]only\b", re.I),
    re.compile(
        r"\bi\s+(?:can\s+only|only)\s+"
        r"(?:communicate|chat|talk|respond)\s+"
        r"(?:in|via|through|by)\s+text\b", re.I),
    # "I can only read text" (Tab round 6): the read/see
    # shapes of the same false claim — the app transcribes
    # voice to text and answers aloud in Voice Mode.
    re.compile(
        r"\bi\s+(?:can\s+only|only)\s+"
        r"(?:read|see|receive|process|understand)\s+"
        r"(?:text|messages|typed\s+messages)\b", re.I),
    re.compile(
        r"\bi\s+(?:can't|cannot|can\s+not)\s+"
        r"(?:receive|hear|process|accept)\s+audio\b", re.I),
    re.compile(
        r"\bi\s+(?:can't|cannot|can\s+not)\s+"
        r"(?:hear|listen\s+to)\s+(?:voice|audio|your\s+voice|"
        r"recordings?)\b", re.I),
    re.compile(
        r"\bno\s+audio\s+(?:input|support|option)\b", re.I),
    re.compile(r"אין\s+לי\s+אפשרות\s+לשמוע"),
    re.compile(r"אני\s+לא\s+יכול(?:ה)?\s+לשמוע\s+אותך"),
    re.compile(
        r"אני\s+לא\s+יכול(?:ה)?\s+לשמוע\s+"
        r"(?:הודעות|הקלטות|אודיו|קול)"),
    re.compile(
        r"אני\s+יכול(?:ה)?\s+רק\s+לקרוא\s+(?:טקסט|הודעות)"),
    re.compile(r"רק\s+בטקסט"),
    re.compile(r"רק\s+טקסט"),
    re.compile(r"תקשורת\s+טקסטואלית\s+בלבד"),
]


def _sentence_voice_denial(seg):
    """True when one sentence denies voice/audio capability
    the app actually has."""
    return any(rx.search(seg) for rx in _VOICE_DENIAL_RES)


# Invented-worry openers (Tab round 5): a Hebrew greeting
# ("היי") got the opener "ראיתי שיש משהו שמעיק עליך" — "I
# saw that something is troubling you" — a problem the
# person never stated. The prompt now forbids it, and this
# scrub is the code wall: when the person's message states
# NO problem, a FIRST sentence claiming to see / sense an
# unstated trouble is dropped. When they did state one
# (including a loss), the reply may reflect it — the guard
# stays off.
_PROBLEM_STATED_RE = re.compile(
    r"(חרד|דואג|דאגה|עצוב|קשה\s+לי|כואב|כאב|בעיה|מעיק|מטריד|"
    r"לחוץ|לחץ|מפחד|פחד|בודד|עייף|מרגיש\s+רע|לא\s+טוב\s+לי|"
    r"נפטר|נפטרה|מת\s+לי|מתה|אובדן|אבא\s+שלי\s+מת|"
    r"\b(?:anxious|anxiety|worried|worry|sad|depress\w*|"
    r"stress\w*|panic|afraid|scared|hurt|pain|angry|lonely|"
    r"overwhelm\w*|bothering|troubling|struggl\w*|problem|"
    r"died|passed\s+away|funeral|grief|feel\s+"
    r"(?:bad|awful|terrible|down|low))\b)",
    re.I)
_INVENTED_WORRY_RES = [
    re.compile(
        r"ראיתי\s+ש(?:יש|היה)\s+[^.!?\n]{0,25}?"
        r"(?:מעיק|מטריד|מכביד)\s+עליך"),
    re.compile(
        r"(?:אני\s+)?(?:רואה|מרגיש|מרגישה|חש|חשה)\s+שיש\s+"
        r"משהו\s+ש(?:מעיק|מטריד|מכביד)"),
    re.compile(
        r"(?:נראה|מרגיש)\s+(?:לי\s+)?ש(?:יש\s+)?משהו\s+"
        r"(?:מעיק|מטריד|מכביד)\s+עליך"),
    re.compile(r"יש\s+משהו\s+ש(?:מעיק|מטריד|מכביד)\s+עליך"),
    re.compile(
        r"\bi\s+can\s+(?:see|sense|tell)\b[^.!?\n]{0,40}?\b"
        r"(?:bothering|troubling|weighing)\b", re.I),
    re.compile(
        r"\bsomething\s+(?:is|seems\s+to\s+be)\s+"
        r"(?:bothering|troubling|weighing\s+on)\s+you\b", re.I),
    re.compile(
        r"\byou\s+seem\s+(?:troubled|worried|upset|"
        r"distressed)\b", re.I),
]


_BARE_GREETING_RE = re.compile(
    r"^(?:שלום|היי|הי|הלו|hi|hello|hey)\b", re.I)


def _drop_invented_worry_opener(text, user_text):
    """Drop an opening sentence that claims to see/sense a
    trouble the person never mentioned. The flagged sentence
    is the reply's opener — either the first sentence itself
    or the one right after a bare greeting ("שלום! ראיתי שיש
    משהו שמעיק עליך."). No-op when no user text is supplied
    (non-chat surfaces) or when the person DID state a
    problem."""
    if not text or user_text is None:
        return text
    if _PROBLEM_STATED_RE.search(user_text or ""):
        return text
    segs = _sentence_segments(text)
    real = [i for i, (s, _) in enumerate(segs) if s.strip()]
    if not real:
        return text
    candidates = [real[0]]
    first_txt = segs[real[0]][0].strip()
    if len(real) > 1 and len(first_txt) <= 40 and \
            _BARE_GREETING_RE.match(first_txt):
        candidates.append(real[1])
    for idx in candidates:
        if any(rx.search(segs[idx][0])
               for rx in _INVENTED_WORRY_RES):
            return "".join(
                s + sep for i, (s, sep) in enumerate(segs)
                if i != idx).strip()
    return text


def _sentence_segments(text):
    """Split into (sentence, separator-after) pairs, losslessly."""
    parts = _SENT_SEP_RE.split(text or "")
    segs = []
    for i in range(0, len(parts), 2):
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        segs.append((parts[i], sep))
    return segs


# A14: the Hebrew calque blocklist — the named broken-Hebrew
# shapes Tab caught (calques and agreement breaks). A sentence
# carrying one is dropped whole; code never edits fragments.
_HE_CALQUE_RES = [
    re.compile(r"תנחומי\w*\s+האמיתיים"),
    re.compile(r"מה\s+עלה\s+לך\s+לב"),
    re.compile(r"מה\s+הוא\s+היה\s+כמו"),
    re.compile(r"לא\s+זזה"),
    re.compile(r"בלי\s+(?:שום\s+)?מהירות"),
    re.compile(r"(?:^|[.!?…]\s+)זה\s+תחושה"),
    # Tab r13 item 6: the two calques Tab caught on device,
    # in their standalone forms — "אין שום מהירות לרפא" (no
    # בלי before it) and "דברו כואב" (a garbled imperative of
    # "speaking hurts"). Whole sentences carrying them are
    # dropped, never shipped.
    re.compile(r"שום\s+מהירות"),
    re.compile(r"דברו\s+כואב"),
    # Tab r14 item 5: three more strings that must never ship —
    # the standalone token דברו (the garbled plural imperative,
    # in any one-letter prefix form), the non-word כזאתם, and
    # the agreement break שאתה שיתף (masculine "you" driving a
    # third-person past verb, e.g. inside "תודה שאתה שיתף").
    re.compile(r"(?<![\u0590-\u05FF])[ושבלכ]?דברו"
               r"(?![\u0590-\u05FF])"),
    re.compile(r"כזאתם"),
    re.compile(r"שאתה\s+שיתף"),
]


def _sentence_calque(seg):
    return any(rx.search(seg or "") for rx in _HE_CALQUE_RES)


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
        if _sentence_banned(seg) or _sentence_availability_claim(seg) \
                or _sentence_approach_claim(seg) \
                or _sentence_voice_denial(seg) \
                or _sentence_calque(seg) \
                or _sentence_dangling(seg):
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


def _sentence_dangling(seg):
    """True for a "What I *can* do…" fragment ANYWHERE in a reply
    (Tab round 4): the filter removed the sentence that carried
    the refusal, and the fragment now also surfaces mid-reply,
    after a greeting ("Welcome, … I'm glad you're here. What I
    *can* do…") — the round-3 backstop only caught it at the
    reply's start. A COMPLETE sentence ("What I can do is
    listen, for as long as you need.") is legitimate and stays:
    the dangling shape never completes with "do is …" or a
    colon list."""
    s = (seg or "").strip()
    if not _DANGLING_OPENER_RE.match(s):
        return False
    if re.search(r"\bdo\s+is\b", s, re.I):
        return False
    if re.search(r"\bdo\s*:", s):
        return False
    return True


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


# Solace's own Hebrew gender (Tab round 4): Solace speaks
# about ITSELF in masculine forms — the fixed strings already
# do ("אני ממש מצטער" in the grief opening and the spouse
# route) — but model replies flipped (שמח / שמחה / שמח/ה).
# Self-referential feminine forms are rewritten to the
# masculine, and gender slash forms are banned in shipped
# output outright (normalized to the masculine stem). Only
# clearly self-referential forms are touched: "אני שמחה"
# (Solace speaking) becomes "אני שמח", while forms addressed
# to the user ("את מרגישה") are the grief layer's business,
# never this one's.
_HE_SELF_FEM_MASC = {
    "שמחה": "שמח", "מצטערת": "מצטער", "מבינה": "מבין",
    "מרגישה": "מרגיש", "יכולה": "יכול", "נמצאת": "נמצא",
    "מוכנה": "מוכן", "בטוחה": "בטוח", "אוהבת": "אוהב",
    "שומעת": "שומע", "יודעת": "יודע", "חושבת": "חושב",
    "מסוגלת": "מסוגל", "אומרת": "אומר", "שואלת": "שואל",
    "כותבת": "כותב", "מדברת": "מדבר", "מבקשת": "מבקש",
    "מספרת": "מספר", "צריכה": "צריך", "עייפה": "עייף",
    "זקוקה": "זקוק", "עוזרת": "עוזר", "מקשיבה": "מקשיב",
    "מעדיפה": "מעדיף", "מציעה": "מציע", "מאמינה": "מאמין",
    "קוראת": "קורא", "פותחת": "פותח", "ממשיכה": "ממשיך",
    "נשארת": "נשאר", "חוזרת": "חוזר",
    # A6's named participles: a bare first-person feminine
    # participle is Solace speaking as a woman — masculine.
    "זמינה": "זמין", "מקצועית": "מקצועי", "מאבחנת": "מאבחן",
    "ממליצה": "ממליץ", "מכירה": "מכיר", "מורשית": "מורשה",
    "מוסמכת": "מוסמך", "נרגשת": "נרגש", "פנויה": "פנוי",
    # Tab r14 item 4: the forms Tab caught on device that the
    # map didn't know — סקרנית (curious) above all, plus the
    # rest of the everyday first-person set.
    "סקרנית": "סקרן", "קשובה": "קשוב", "נלהבת": "נלהב",
    "מתרגשת": "מתרגש", "דואגת": "דואג", "נהנית": "נהנה",
    "מתכוונת": "מתכוון", "חוששת": "חושש",
}
# Round 5: the middle slot now also carries negation and
# small adverbs — "אני לא יכולה" slipped past round 4
# because the regex demanded the form directly after אני
# (or אני ממש). Middle words are preserved verbatim.
# Tab r14 item 4: the slot widens (deictics + a trailing
# comma) — "אני כאן, מקשיבה" and "אני תמיד מבינה" shipped
# feminine because כאן / תמיד / a comma weren't allowed
# between אני and the participle.
_HE_SELF_FEM_RE = re.compile(
    r"(אני)\s+((?:(?:ממש|לא|גם|רק|כבר|תמיד|כאן|פה|עכשיו|"
    r"איתך|איתכם|איתכן)[,\s]+){0,3})("
    + "|".join(_HE_SELF_FEM_MASC) + r")(?![\u0590-\u05FF])")

# Self-reference WITHOUT an explicit אני (Tab round 5):
# "שמחה שפנית" ("(I'm) glad you reached out"), "עוזרת AI"
# ("a (female) AI assistant"), a sentence-initial "מצטערת
# על…" / "שמחה לשמוע". These shapes are Solace describing
# ITSELF; each is rewritten to the masculine.
_HE_SELF_PHRASE_RES = [
    (re.compile(r"שמחה\s+שפנית"), "שמח שפנית"),
    (re.compile(r"עוזרת\s+(AI|בינה)"), r"עוזר \1"),
]
_HE_SELF_START_FORMS = {
    "שמחה": "שמח", "מצטערת": "מצטער", "מבינה": "מבין",
    "מרגישה": "מרגיש", "מקשיבה": "מקשיב", "ממליצה": "ממליץ",
    "מכירה": "מכיר", "יכולה": "יכול", "זמינה": "זמין",
    "מאבחנת": "מאבחן", "עוזרת": "עוזר",
    # r14 item 4: Tab's named forms at a sentence start too.
    "סקרנית": "סקרן", "קשובה": "קשוב", "שואלת": "שואל",
    "נלהבת": "נלהב",
}
_HE_SELF_START_RE = re.compile(
    r"(^|[.!?…]\s+)(" + "|".join(_HE_SELF_START_FORMS) + r")"
    r"(?=\s+(?:ש|ל|על|אותך|אותו|אותה))", re.M)

# "Glad you came" without אני and NOT at a sentence start
# (Tab round 6): the start-form rule above needs sentence
# punctuation before it, so the greeting shapes that follow
# a greeting word or a comma shipped feminine — "היי,
# שמחה שבאת", "שלום, שמחה שהגעת", "אני כאן בשבילך, שמחה
# שאתה כאן" (8 of 44 Hebrew replies). Wherever שמחה is
# immediately followed by a ש־ clause (optionally via
# מאוד / ממש / כל כך), it is Solace being glad — masculine.
_HE_GLAD_THAT_RE = re.compile(
    r"(^|[.!?…]\s+|,\s+|(?:שלום|היי|הי|הלו)[!,]?\s+)"
    r"שמחה(?=\s+(?:ממש\s+|מאוד\s+|כל\s+כך\s+)?"
    r"ש[\u0590-\u05FF])", re.M)


def _glad_that_sub(m):
    return m.group(1) + "שמח"


def _self_start_sub(m):
    return m.group(1) + _HE_SELF_START_FORMS[m.group(2)]


def _self_fem_sub(m):
    return (m.group(1) + " " + (m.group(2) or "")
            + _HE_SELF_FEM_MASC[m.group(3)])
_HE_SLASH_FORM_RE = re.compile(
    r"([\u0590-\u05FF]{2,})/(ה|ת|ית)(?![\u0590-\u05FF])")
_HE_SLASH_PAIR_RE = re.compile(
    r"([\u0590-\u05FF]{2,})/([\u0590-\u05FF]{2,})")


def _slash_pair_sub(m):
    """Full-form slash pair ("שמח/שמחה", "אתה/את"): when the
    two words are one stem and its gendered variant (a shared
    prefix of 2+ letters), keep the first — the masculine, in
    the order models write them. The first word may carry up
    to two glued prefix letters ("כשאתה/את"). Unrelated pairs
    ("יום/לילה") are untouched."""
    a, b = m.group(1), m.group(2)
    for drop in (0, 1, 2):
        x = a[drop:]
        common = 0
        for p, q in zip(x, b):
            if p != q:
                break
            common += 1
        if common >= 2 and len(x) - common <= 2:
            return a
    return m.group(0)


# Solace's Hebrew name (Tab round 5): the app's shipped
# Hebrew strings spell it סולאס (identity + availability
# answers). The model sometimes writes סולייס instead — one
# spelling ships, normalized here on every scrub surface.
_HE_NAME_VARIANT = "סולייס"
_HE_NAME_PINNED = "סולאס"
_LATIN_NAME_RE = re.compile(r"\bSolace\b")


def _he_name_in_hebrew(text):
    """Inside Hebrew text the name is written סולאס, never
    Latin "Solace" (Tab round 6) — including the approved
    voice-privacy line, whose Hebrew form Tab re-pinned
    this round. The rule is per-sentence: a Hebrew sentence
    naming "Solace" ships סולאס; an English sentence keeps
    "Solace" untouched."""
    if not text or "Solace" not in text:
        return text
    segs = _sentence_segments(text)
    parts = []
    for seg, sep in segs:
        if has_hebrew(seg):
            seg = _LATIN_NAME_RE.sub(_HE_NAME_PINNED, seg)
        parts.append(seg + sep)
    return "".join(parts)

# User-addressed imperative slash forms (Tab round 5):
# "ספר/י לי" SHIPPED in round 4 — the suffix regexes only
# knew ה/ת/ית, never the imperative י. Slash imperatives now
# resolve by the WRITER's detected gender: a woman writing
# gets the י form ("ספרי לי"), everyone else the bare stem
# ("ספר לי"). Full-pair variants (כתוב/כתבי, אתה/את) resolve
# to the feminine side only for a feminine writer; Solace's
# OWN slash pairs (שמח/שמחה) are never gendered-variant
# shaped (the feminine side ends in ה) and keep resolving
# to the masculine below.
_HE_SLASH_IMP_Y_RE = re.compile(
    r"([\u0590-\u05FF]{2,})/י(?![\u0590-\u05FF])")
_HE_SLASH_PAIR_ANY_RE = re.compile(
    r"([\u0590-\u05FF]{2,})/([\u0590-\u05FF]{2,})"
    r"(?![\u0590-\u05FF])")


def _resolve_slashes_for_writer(text, writer_gender):
    """Resolve USER-addressed slash forms toward the writer's
    gender before the generic (masculine) slash stripping."""
    if writer_gender == "f":
        text = _HE_SLASH_IMP_Y_RE.sub(r"\1י", text)

        def _pick_fem(m):
            a, b = m.group(1), m.group(2)
            common = 0
            for p, q in zip(a, b):
                if p != q:
                    break
                common += 1
            if common >= 2 and abs(len(a) - len(b)) <= 2 and (
                    b.endswith("י") or b in ("את", "שאת")):
                return b
            return m.group(0)

        return _HE_SLASH_PAIR_ANY_RE.sub(_pick_fem, text)
    return _HE_SLASH_IMP_Y_RE.sub(r"\1", text)


# "לספרי" is never the infinitive (Tab round 6): the model
# writes "אפשר לספרי לי" and the round-5 slash resolver
# could mint it for a woman from "לספר/י" — but the
# infinitive of "to tell" is לספר for EVERY writer (the
# feminine imperative is the bare ספרי, without the ל).
# Where לספרי sits in the infinitive slot (before לי / לך /
# עוד / על / את / מה … or at a sentence end), it ships as
# לספר. Shared by the scrub layer and the grief gender
# consistency, so no surface can ship it.
_HE_LESAFERI_RE = re.compile(
    r"לספרי(?=\s+(?:לי|לך|לו|לה|לנו|לכם|לכן|עוד|על|את|מה|"
    r"קצת)(?![\u0590-\u05FF])|[.!?…\"']|\s*$)", re.M)


def _fix_infinitive_y(text):
    """לספרי in the infinitive slot -> לספר (Tab round 6)."""
    if not text:
        return text
    return _HE_LESAFERI_RE.sub("לספר", text)


def _he_self_gender(text, writer_gender=None):
    """Normalize Solace's OWN Hebrew gender in a model reply:
    slash forms lose the slash (masculine stem), and feminine
    first-person forms after אני become masculine. Text with
    no Hebrew passes through untouched. `writer_gender`
    (round 5) only steers USER-addressed imperative slashes —
    Solace's own forms are masculine regardless."""
    if not text or not has_hebrew(text):
        return text
    out = text.replace(_HE_NAME_VARIANT, _HE_NAME_PINNED)
    out = _he_name_in_hebrew(out)
    out = _resolve_slashes_for_writer(out, writer_gender)
    out = out.replace("הוא/היא", "הוא").replace("היא/הוא", "הוא")
    out = _HE_SLASH_FORM_RE.sub(r"\1", out)
    out = _HE_SLASH_PAIR_RE.sub(_slash_pair_sub, out)
    if writer_gender != "f":
        # A bare feminine imperative addressed to a man /
        # no-signal writer ships in its plain form (Tab
        # round 6) — the grief layer has always done this;
        # the general scrub now matches it. A woman keeps
        # ספרי.
        out = re.sub(r"ספרי\s+לי", "ספר לי", out)
    out = _fix_infinitive_y(out)
    for rx, rep in _HE_SELF_PHRASE_RES:
        out = rx.sub(rep, out)
    out = _HE_SELF_START_RE.sub(_self_start_sub, out)
    out = _HE_GLAD_THAT_RE.sub(_glad_that_sub, out)
    return _HE_SELF_FEM_RE.sub(_self_fem_sub, out)


def _drop_wrong_script_sentences(out, user_text):
    """A21: the reply stays in the main language of the person's
    message — a full sentence in the other script is dropped,
    never shipped as a mid-reply language switch."""
    if not out or not user_text:
        return out
    he = len(re.findall(r"[\u0590-\u05FF]", user_text))
    en = len(re.findall(r"[A-Za-z]", user_text))
    if he == en:
        return out
    want_he = he > en
    segs = _sentence_segments(out)
    kept = []
    for s, sep in segs:
        words = s.split()
        if len(words) >= 4:
            s_he = len(re.findall(r"[\u0590-\u05FF]", s))
            s_en = len(re.findall(r"[A-Za-z]", s))
            if want_he and s_en > 0 and s_he == 0:
                continue
            if not want_he and s_he > 0 and s_en == 0:
                continue
        kept.append((s, sep))
    if len(kept) == len(segs):
        return out
    return "".join(s + sep for s, sep in kept).strip() or out


def scrub_reply(text, writer_gender=None, user_text=None):
    """The full-text filter applied to every finished model reply."""
    out = _scrub_join(
        _sentence_segments(_truncate_glued_reply(text)),
        include_last=True)
    out = _drop_dangling_opener(out.strip())
    out = _drop_invented_worry_opener(out, user_text)
    out = _he_self_gender(out, writer_gender)
    # A15 + A5: no Hebrew slash pair ever ships, and second-person
    # forms match the writer's stored gender (neutral until known).
    out = strip_hebrew_slashes(out, writer_gender)
    out = he_gender_repair(out, writer_gender)
    out = _drop_wrong_script_sentences(out, user_text)
    if out:
        return out
    if has_hebrew(text or ""):
        return _SCRUB_FALLBACK_HE
    return _SCRUB_FALLBACK


def scrub_prefix(text, writer_gender=None, user_text=None):
    """Streaming form: the scrubbed text of all COMPLETE sentences
    so far — safe to emit as a delta; the tail is still growing."""
    out = _drop_dangling_opener(
        _scrub_join(
            _sentence_segments(_truncate_glued_reply(text)),
            include_last=False))
    out = _drop_invented_worry_opener(out, user_text)
    out = _he_self_gender(out, writer_gender)
    out = strip_hebrew_slashes(out, writer_gender)
    return he_gender_repair(out, writer_gender)


def scrub_field(text, writer_gender=None):
    """Debrief-field form: like scrub_reply but may return "" so the
    caller can substitute its own neutral default."""
    out = _he_self_gender(_scrub_join(
        _sentence_segments(text), include_last=True).strip(),
        writer_gender)
    out = strip_hebrew_slashes(out, writer_gender)
    return he_gender_repair(out, writer_gender)


_CONDOLENCE_LEAD_RE = re.compile(
    r"(אני (?:ממש |כל כך )?מצטער(?:ת)?|משתתף בצער|תנחומיי|"
    r"שלא תדעו עוד צער|"
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
    prepends the one true opening itself. Tab r13 item 6: an
    echo sentence is dropped wherever it appears in the body,
    not only at the start — a follow-up must never repeat the
    opening, in whole or in part."""
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
    # Mid-body echoes (only when something else remains).
    for want in open_segs:
        segs = _sentence_segments(body)
        kept = [(s, sep) for s, sep in segs if s.strip() != want]
        if len(kept) == len(segs) or not kept:
            continue
        body = "".join(s + sep for s, sep in kept).strip()
    return body


# A condolence phrased as if hearing the news for the first time
# ("sorry to HEAR about your loss", "מצטער על האובדן") — distinct
# from a mid-reply "I'm sorry you're going through this". One of
# these AFTER the body's opening sentences means a second reply
# was glued on (Tab round 3).
_SECOND_REPLY_LEAD_RE = re.compile(
    r"(אני (?:ממש |כל כך )?מצטער(?:ת)? (?:לשמוע|על (?:ה)?אובדנ)|"
    r"משתתף בצער|"
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


# Second-opening stripper (Tab round 4): even with the
# round-2/3 cleanup, the model's Hebrew grief continuation
# still OPENED with its own opener in 8 of 8 runs — a presence
# line with the person's name ("אני כאן איתך, דני.") or a
# fresh condolence in a new wording ("עמוק מאוד מצטער על
# אובדן אביך."). The FIXED opening already does condolence +
# presence + take-your-time, so any sentence performing those
# functions among the continuation's opening sentences is a
# second opening and goes. (Mid-reply sentences are safe: only
# the first few sentences are scanned, and only opener-shaped
# ones are dropped.)
_OPENER_SENTENCE_RES = [
    # condolence on the loss, any wording that names it
    # (אובדנ with a regular nun: possessives — אובדנך, אובדנו —
    # respell the final ן, the r9 final-nun trap)
    re.compile(
        r"(?:מצטער|מצטערת)[^.!?\n]{0,30}?"
        r"(?:אובדן|אובדנ|לשמוע|צער|שאיבדת)"),
    re.compile(r"משתתף בצער|תנחומיי|שלא תדעו עוד צער"),
    re.compile(
        r"\bsorry\s+(?:for\s+your\s+loss|to\s+hear)\b", re.I),
    re.compile(r"\bmy\s+condolences\b", re.I),
    # presence openers ("I'm here with you" — incl. a name)
    re.compile(r"אני\s+כאן\s+(?:איתך|בשבילך|לצידך)"),
    re.compile(r"\bi'?m\s+here\s+with\s+you\b", re.I),
    re.compile(r"\bi\s+am\s+here\s+with\s+you\b", re.I),
]


def _is_opener_sentence(seg):
    s = (seg or "").strip()
    if not s:
        return False
    if any(rx.search(s) for rx in _OPENER_SENTENCE_RES):
        return True
    # A bare short greeting ("שלום דני.") opens a new reply too.
    if len(s) <= 40 and re.match(
            r"^(?:שלום|היי|הי|הלו)\b", s):
        return True
    if len(s) <= 40 and re.match(
            r"^(?:hi|hello|hey)\b", s, re.I):
        return True
    return False


# Companionship lines (Tab round 5): round 4 stripped opener-
# shaped sentences only among the continuation's FIRST three
# sentences, but the model's "my heart / my thoughts / we're
# with you" lines kept shipping — 9 of 13 runs, every female-
# writer run — sometimes mid-body. The FIXED opening already
# carries the companionship ("אני כאן איתך"), so a sentence of
# this family ANYWHERE in the grief body is a second opening
# and goes. The deceased's forms (עליו / עליה) never match
# these patterns and stay untouched.
_COMPANIONSHIP_RES = [
    re.compile(r"לבי\s+אית[ךכ]"),          # לבי איתך / איתכם
    re.compile(r"ליבי\s+אית[ךכ]"),         # ליבי איתך
    re.compile(r"הלב\s+שלי\s+אית[ךכ]"),    # הלב שלי איתך
    # "Our heart is with you" (Tab round 6): הלב שלנו איתך
    # shipped once on screen — the plural-possessive heart
    # belongs to the same family as לבי / ליבנו.
    re.compile(r"(?:ה)?לב\s+שלנו\s+(?:אית[ךכ]|אליך|אלייך)"),
    re.compile(r"ליבנו\s+אית[ךכ]"),
    re.compile(r"ליבנו\s+(?:אליך|אלייך|אית[ךכ])"),
    re.compile(r"לבנו\s+(?:אליך|אלייך|אית[ךכ])"),
    re.compile(r"(?:ה)?מחשבות\s+(?:שלי|שלנו|שליי)\s+אית[ךכ]"),
    re.compile(r"מחשבותיי\s+אית[ךכ]"),
    re.compile(r"(?:ה)?מחשבות\s+(?:שלי|שלנו)\s+(?:הולכות\s+)?אליך"),
    re.compile(
        r"(?:אנחנו|אנו)\s+(?:כאן\s+)?"
        r"(?:אית[ךכ]|לציד[ךכ]|בשביל[ךכ])"),
    re.compile(r"(?:אנחנו|אנו)\s+כאן\s+אית[ךכ]"),
    re.compile(r"\bmy\s+heart\s+is\s+with\s+you\b", re.I),
    re.compile(r"\bmy\s+thoughts\s+are\s+with\s+you\b", re.I),
    re.compile(r"\bwe(?:'re|\s+are)\s+(?:all\s+)?here\s+"
               r"(?:with|for)\s+you\b", re.I),
    # A7: Solace never says "we" for itself in grief — no
    # "we're with you", no "our hearts".
    re.compile(r"\bwe(?:'re|\s+are)\s+(?:right\s+)?with\s+you\b",
               re.I),
    re.compile(r"\bour\s+(?:hearts?|thoughts)\b[^.!?\n]{0,30}?"
               r"\bwith\s+you\b", re.I),
]

# A7: the grief continuation contains NO sympathy / condolence
# language at all — the fixed opening already said it. The
# lexicon runs over EVERY sentence of the continuation, not just
# its openers (earlier rounds stripped leads only, and mid-body
# condolences kept shipping).
_SYMPATHY_ANY_RE = re.compile(
    r"(sorry\s+for\s+your\s+loss|sorry\s+to\s+hear|"
    r"my\s+(?:deepest\s+)?condolences|deepest\s+sympath|"
    r"i'?m\s+so\s+sorry\s+(?:for|about|to\s+hear)|"
    # Tab r13 item 6: the bare forms are sympathy too — a
    # grief CONTINUATION may not open with (or contain) a
    # standalone "I'm so sorry", "so sorry", or "my heart
    # goes out to you" at all.
    r"i'?m\s+so\s+sorry\b|"
    r"\bso\s+sorry\b|"
    r"my\s+heart\s+goes\s+out|"
    r"we'?re\s+(?:so\s+)?sorry|"
    # A7's lexicon names them: "take your time" and "with you"
    # companionship are sympathy language in a continuation.
    r"take\s+(?:all\s+)?(?:the|your)\s+time|"
    r"מצטער(?:ת)?\s+(?:מאוד\s+|ממש\s+)?(?:על\s+(?:ה)?אובדן|לשמוע)|"
    r"משתתפ(?:ת|ים)?\s+בצער|תנחומיי|תנחומים|שלא\s+תדעו\s+עוד\s+צער|"
    r"מנחם\s+את|משתתף\s+בצערך|"
    # NOTE: "קח את הזמן" is NOT stripped in Hebrew — the pinned
    # openings themselves say "קח את כל הזמן שאתה צריך", and
    # Tab's round-10 example pins "קחי את הזמן שאת צריכה" as a
    # KEPT continuation sentence. The EN "take your time" stays.
    r"איתך\s+בזמן\s+הזה|לב\s+כבד|שומע\s+על\s+הפטירה|"
    r"על\s+(?:ה)?פטירה\s+של)",
    re.I,
)


def _strip_sympathy_sentences(body):
    """A7: remove every sentence carrying sympathy / condolence
    language from a grief continuation, wherever it appears."""
    if not body:
        return body
    segs = _sentence_segments(body)
    kept = [(s, sep) for s, sep in segs
            if not _SYMPATHY_ANY_RE.search(s)]
    if len(kept) == len(segs):
        return body
    return "".join(s + sep for s, sep in kept).strip()


# Tab r14 item 5: in a grief FOLLOW-UP, the opening's own two
# signature phrases are repeats too — "no right way" (the EN
# opening: "there is no right way to grieve") and "קח את הזמן"
# (the HE opening's "קח את כל הזמן שאתה צריך", any imperative
# form). The r13 no-repeat rule covered the opening as a whole;
# Tab extends it to these phrases. Follow-ups only: a FIRST
# reply's continuation keeps its pinned behavior (round 10
# keeps "קחי את הזמן שאת צריכה" in a first-turn cleanup).
_GRIEF_REPEAT_PHRASE_RES = [
    re.compile(r"no\s+right\s+way", re.I),
    re.compile(r"קח(?:י)?\s+את\s+(?:כל\s+)?הזמן"),
]


def _strip_grief_repeat_phrases(body):
    """Drop every sentence carrying a repeated opening phrase
    from a grief follow-up's continuation."""
    if not body:
        return body
    segs = _sentence_segments(body)
    kept = [(s, sep) for s, sep in segs
            if not any(rx.search(s)
                       for rx in _GRIEF_REPEAT_PHRASE_RES)]
    if len(kept) == len(segs):
        return body
    return "".join(s + sep for s, sep in kept).strip()


def _strip_companionship_lines(body):
    """Remove every 'my heart / my thoughts / we're with you'
    sentence from a grief continuation, wherever it appears —
    not just at the start (Tab round 5)."""
    if not body:
        return body
    segs = _sentence_segments(body)
    kept = [(s, sep) for s, sep in segs
            if not any(rx.search(s) for rx in _COMPANIONSHIP_RES)]
    if len(kept) == len(segs):
        return body
    return "".join(s + sep for s, sep in kept).strip()


def _strip_continuation_openers(body):
    """Drop opener-shaped sentences (condolence / presence /
    greeting) among the continuation's first three sentences."""
    if not body:
        return body
    segs = _sentence_segments(body)
    real = [i for i, (s, _) in enumerate(segs) if s.strip()]
    if not real:
        return body
    drop = set()
    for i in real[:3]:
        if _is_opener_sentence(segs[i][0]):
            drop.add(i)
    if not drop:
        return body
    return "".join(
        s + sep for i, (s, sep) in enumerate(segs)
        if i not in drop).strip()


# Hebrew gender consistency (Tab round 3, target made
# bidirectional in round 4): the model's grief continuation
# flipped the addressee's gender mid-reply ("קח… שאתה צריך"
# in one half, "שאת מרגישה… תרצי" in the other). Unambiguous
# SECOND-PERSON forms are rewritten toward the TARGET gender
# — the writer's own gender when their message revealed it
# (round 4: a woman writing is addressed as a woman), else
# the fixed opening's masculine. Forms about the person who
# died (היא, עליה, שלה / הוא, עליו, שלו) are never touched.
# A sentence still carrying the wrong gender's marker after
# the rewrite is dropped rather than shipped misgendered:
# one reply, one gender.
_HE_FEM_VERB_MASC = {
    "מרגישה": "מרגיש", "צריכה": "צריך", "יכולה": "יכול",
    "חושבת": "חושב", "יודעת": "יודע", "נמצאת": "נמצא",
    "מסוגלת": "מסוגל", "אומרת": "אומר", "שואלת": "שואל",
    "כותבת": "כותב", "מדברת": "מדבר", "מבקשת": "מבקש",
    "מספרת": "מספר", "רוצה": "רוצה", "עוברת": "עובר",
    "מבינה": "מבין",
}
_HE_FEM_VERB_ALT = "|".join(_HE_FEM_VERB_MASC)
_HE_FEM2P_RE = re.compile(
    r"(שאת|את)\s+(?:" + _HE_FEM_VERB_ALT + r")")
_HE_FEM2P_LEFTOVER_RE = re.compile(
    r"(שאת\b|תרצי|תרגישי|תקחי|קחי\b|ספרי\s+לי|"
    r"את\s+(?:" + _HE_FEM_VERB_ALT + r"))")
_HE_MASC_VERB_FEM = {v: k for k, v in _HE_FEM_VERB_MASC.items()
                     if v != k}
_HE_MASC_VERB_ALT = "|".join(_HE_MASC_VERB_FEM)
_HE_MASC2P_RE = re.compile(
    r"(שאתה|אתה)\s+(?:" + _HE_MASC_VERB_ALT + r")")
_HE_MASC2P_LEFTOVER_RE = re.compile(
    r"(שאתה\b|תרצה(?![\u0590-\u05FF])|תרגיש(?![\u0590-\u05FF])|"
    r"תקח(?![\u0590-\u05FF])|(?<![\u0590-\u05FF])קח(?![\u0590-\u05FF])|"
    # The bare imperative only: inside לספר ("to tell")
    # the same letters are the infinitive, correct for
    # every gender — never a leftover (Tab round 6).
    r"(?<![\u0590-\u05FF])ספר\s+לי|"
    r"אתה\s+(?:" + _HE_MASC_VERB_ALT + r"))")


def _he_gender_consistency(body, target="m"):
    """Normalize the continuation's second-person forms toward
    `target` ("f" = the writer is a woman, "m" = masculine —
    the fixed opening's gender)."""
    if not body:
        return body
    # לספרי in the infinitive slot is wrong for EVERY
    # target (Tab round 6) — normalize before the gender
    # rewrites, whose own ספרי forms are the imperative.
    body = _fix_infinitive_y(body)
    if target == "f":
        if not _HE_MASC2P_LEFTOVER_RE.search(body):
            return body

        def _verb_sub_f(m):
            verb = m.group(0).split(None, 1)[1]
            pron = "שאת" if m.group(1) == "שאתה" else "את"
            return pron + " " + _HE_MASC_VERB_FEM[verb]

        out = _HE_MASC2P_RE.sub(_verb_sub_f, body)
        # Bare imperative only — the same letters inside
        # לספר are the infinitive and stay (Tab round 6:
        # this substitution was itself minting לספרי).
        out = re.sub(r"(?<![\u0590-\u05FF])ספר\s+לי",
                     "ספרי לי", out)
        out = re.sub(r"תרצה(?![\u0590-\u05FF])", "תרצי", out)
        out = re.sub(r"תרגיש(?![\u0590-\u05FF])", "תרגישי", out)
        out = re.sub(r"תקח(?![\u0590-\u05FF])", "תקחי", out)
        out = re.sub(r"(?<![\u0590-\u05FF])קח(?![\u0590-\u05FF])",
                     "קחי", out)
        out = re.sub(r"שאתה\b", "שאת", out)
        leftover = _HE_MASC2P_LEFTOVER_RE
    else:
        if not _HE_FEM2P_LEFTOVER_RE.search(body):
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
        leftover = _HE_FEM2P_LEFTOVER_RE
    segs = _sentence_segments(out)
    kept = [(s, sep) for s, sep in segs
            if not leftover.search(s)]
    if len(kept) != len(segs):
        out = "".join(s + sep for s, sep in kept).strip()
    return out


def _grief_clean_body(body, opening, writer_gender=None,
                      repeat=False):
    """The deterministic cleanup every grief continuation passes
    through (sync, stream-visible, saved text): no divider seams,
    no echoed opening, no leading condolence pile-up, no second
    opening sentence, no glued-on second reply, one consistent
    gender of address — so the final reply is the fixed opening
    + ONE continuation. `repeat` marks a FOLLOW-UP turn (r13):
    its continuation additionally loses the opening's signature
    phrases (r14 item 5)."""
    if not body:
        return body
    body = _DIVIDER_LINE_RE.sub("", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    body = _strip_opening_echo(body, opening)
    body = _strip_dup_condolence(body)
    body = _strip_continuation_openers(body)
    body = _strip_companionship_lines(body)
    body = _strip_sympathy_sentences(body)
    if repeat:
        body = _strip_grief_repeat_phrases(body)
    body = _cut_second_reply_block(body, opening)
    if opening and has_hebrew(opening):
        # The continuation matches the OPENING that actually
        # shipped: writer-gendered openings carry their gender;
        # a neutral opening (A5, gender unknown) means the
        # continuation is neutralized to the plural, never
        # defaulted to masculine.
        if writer_gender in ("f", "m"):
            target = writer_gender
        elif opening in (GRIEF_OPENING_HE_WF, GRIEF_OPENING_HE_WF_F):
            target = "f"
        elif opening in (GRIEF_OPENING_HE_N, GRIEF_OPENING_HE_N_F):
            target = "n"
        else:
            target = "m"
        if target in ("f", "m"):
            body = _he_gender_consistency(body, target)
        else:
            body = he_gender_repair(body, "n")
    body = body.strip()
    if opening and body and body in opening:
        # Nothing left but a fragment of the opening itself.
        return ""
    return body


def _grief_stream_visible(raw_text, opening, started,
                          writer_gender=None, repeat=False):
    """The visible continuation during a grief STREAM: the scrubbed
    prefix, cleaned exactly like the final body. Held back ("")
    until two complete sentences exist — the condolence strip
    needs the second sentence to know the first was the model's
    own repeat, and emitting that first sentence live is what
    glued two replies together on screen."""
    scrubbed = scrub_prefix(raw_text, writer_gender)
    if not started:
        segs = [s for s, _ in _sentence_segments(scrubbed) if s.strip()]
        if len(segs) < 2:
            return "", False
    return _grief_clean_body(
        scrubbed, opening, writer_gender, repeat), True


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
        # Tab r13 item 10: the memory note follows the
        # conversation's language. A Hebrew conversation gets a
        # Hebrew note and Hebrew ITEMS — the whole first-run
        # experience is in the person's language, not just the
        # replies.
        _he_user = sum(
            1 for r in rows if r["role"] == "user"
            and HEBREW_RE.search(r["content"] or ""))
        _all_user = sum(1 for r in rows if r["role"] == "user")
        lang_note = ""
        if _all_user and _he_user * 2 >= _all_user:
            lang_note = (
                "\n\nLANGUAGE: this conversation is in Hebrew. "
                "Write the note itself in Hebrew, and write "
                "every ITEMS text in Hebrew too."
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
               transcript, rehearsal_note + deleted_note + lang_note)
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
        # The tombstone guard runs the same-theme JUDGE — a model
        # call, seconds slow. It must be computed BEFORE the
        # write transaction opens (Tab round 5): previously the
        # judge ran INSIDE the `with db_lock` write below, so a
        # background memory update could hold the global DB lock
        # — and an open SQLite write transaction — for seconds
        # at a time, and the deterministic crisis fast path
        # (which takes db_lock in the event loop) stalled behind
        # it exactly when many chats started at once. Now the
        # locked section is pure SQL again, milliseconds.
        kept_items = None
        if items is not None:
            sigs = _rehearsal_signatures(rehearsal_rows)
            kept_items = []
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
                kept_items.append((kind, it["text"]))
        with db_lock:
            conn = _conn()
            conn.execute(
                "INSERT INTO summaries (user_id, summary, updated_at) VALUES (?,?,datetime('now')) "
                "ON CONFLICT(user_id) DO UPDATE SET summary=excluded.summary, updated_at=datetime('now')",
                (user_id, new_summary),
            )
            if kept_items is not None:
                conn.execute(
                    "DELETE FROM memory_items WHERE user_id=?", (user_id,)
                )
                for kind, text in kept_items:
                    conn.execute(
                        "INSERT INTO memory_items (user_id, kind, text) VALUES (?,?,?)",
                        (user_id, kind, text),
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
    # Tab r13 item 9: every user id this DEVICE has ever used —
    # "New chat" mints a fresh id, and deleting only the newest
    # one left every older chat on the server. The client sends
    # its full device id list; the endpoint deletes and then
    # VERIFIES every one of them before it may say "deleted".
    user_ids: list[str] | None = None


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
    # A13: an empty concern is OMITTED from the profile the model
    # receives — never stored as an empty "weighing on them"
    # segment, and a "not specified" mood is dropped with it.
    notes = re.sub(
        r"\s*What is weighing on them:\s*$", "", notes)
    notes = re.sub(
        r"Feeling right now:\s*not specified\.?\s*", "", notes
    ).strip(" .")
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
                    lang_v=None, gender_v=None, checkin_v=None):
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
    if gender_v is not None:
        sets.append("writer_gender=?")
        vals.append(gender_v)
    if checkin_v is not None:
        sets.append("crisis_checkin=?")
        vals.append(1 if checkin_v else 0)
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
    # A plain prescribed-medication statement ("לקחתי את
    # הכדורים שלי") is not-crisis BY RULE (Tab round 4): it
    # disarms the overdose detector and skips the classifier —
    # the classifier's 4-of-9 false escalations end here. A
    # keyword crisis phrase in the same message still wins.
    med_safe = is_prescribed_med_statement(msg)
    # "Feeling stuck in life" is not-crisis BY RULE (Tab round
    # 6): it disarms the classifier exactly like the med guard.
    # A keyword crisis phrase in the same message still wins.
    stuck_safe = is_stuck_in_life_statement(msg)
    # Tab FINAL order A1: a NEVER-CRISIS idiom (joke / lyric /
    # quote / school frame) is not-crisis BY RULE, checked before
    # the classifier — the classifier never gets to escalate it.
    idiom_safe = is_never_crisis_idiom(msg)
    # Tab r13 item 3: vanish / disappear / done-with-everything
    # with no life-or-death word is not-crisis BY RULE — the
    # "(wish|want) … disappear" keyword pattern no longer gets
    # to latch it, and the classifier is skipped for it.
    vanish_safe = is_vanish_only_statement(msg)
    # Tab FINAL order A3: the MEDICAL-EMERGENCY check is separate
    # and runs BEFORE the suicide check — but a plain prescribed-
    # medication statement is guarded out of it, as before.
    medical_kind = None if med_safe else detect_medical_emergency(msg)
    emergency = detect_overdose(msg) and not med_safe \
        and not stuck_safe
    # Tab FINAL order A5: the writer's gender — detected from
    # this message's first-person Hebrew, inferred from a known
    # first name, or read from the stored value; stored the
    # moment it is learned, and it steers every fixed string and
    # scrub decision below. Neutral forms until it is known.
    stored_gender = (profile.get("writer_gender") or "") or None
    detected_gender = detect_writer_gender_he(msg)
    if not detected_gender:
        detected_gender = _gender_from_name(profile.get("name"))
    writer_gender = detected_gender or stored_gender
    if detected_gender and detected_gender != stored_gender:
        _set_user_state(uid, gender_v=detected_gender)
    info_q = (not kw_risk) and is_informational_word_question(msg)
    if idiom_safe or vanish_safe:
        # A1 wins over the keyword layer: the idiom accounts for
        # the dark words in the message (checked in the detector).
        # The vanish guard (r13) likewise: the disappear words
        # are the message's only signal (checked in its detector).
        risk = False
    elif kw_risk:
        risk = True
    elif emergency:
        # The emergency branch below returns before risk is ever
        # consulted — and NO model call may precede the 911 /
        # מד״א 101 reply (Tab round 3: the crisis reply must not
        # wait behind model work).
        risk = False
    elif medical_kind:
        # A3: the medical branch below returns first, too.
        risk = False
    elif med_safe:
        risk = False
    elif stuck_safe:
        risk = False
    elif idiom_safe:
        risk = False
    elif detect_clarification(msg):
        # A walk-back (joking / didn't mean it / I'm safe /
        # התכוונתי ש…) is not-crisis BY RULE: the classifier
        # never gets to escalate it past the A2 clarification
        # branch below (Tab r13: the live classifier did
        # exactly that to a Hebrew tiredness retraction).
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
    # A deterministically-not-crisis turn (a prescribed-med
    # statement, a feeling-stuck statement) WINS OVER THE
    # LATCH (Tab round 6): the false alarm had left the chat
    # in crisis mode, and the next ordinary message — "לקחתי
    # את הכדורים שלי" — was answered with the מד״א 101
    # emergency reply. Such a turn is answered NORMALLY, and
    # the latch it disproves is cleared, so the chat is a
    # normal chat again from this message on. (A fresh risk
    # signal or a completed overdose was already handled
    # above and never reaches this bypass.)
    if in_crisis_mode and (med_safe or stuck_safe or idiom_safe
                           or vanish_safe) \
            and not risk and not emergency and not medical_kind \
            and not detect_clarification(msg):
        _set_user_state(uid, crisis_mode=False, stepdown_v=False,
                        lang_v=safety_lang)
        in_crisis_mode = False
        stepdown = False
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
        "med_statement": med_safe,
        "stuck_statement": stuck_safe,
        "vanish_statement": vanish_safe,
        # The writer's own gender (Tab round 5, FINAL order A5):
        # stored per user, neutral until known — steers every
        # gendered fixed string and the scrub layer's morphology.
        "writer_gender": writer_gender,
        # Effective gender for the scrub layer: the A5 plural-
        # neutral ("n") while the writer's gender is unknown.
        "eff_gender": writer_gender if writer_gender in ("m", "f")
        else "n",
        # A2: the ONE gentle check-in line owed on the first
        # normal turn after a true crisis (set in the latch
        # block below); prepended by code in _finish_model_turn.
        "checkin_line": None,
    }

    # Tab FINAL order A3: MEDICAL EMERGENCY — checked BEFORE the
    # suicide path. First line is the call to 911 / מד״א 101, no
    # "tonight?" question, banner on, no model call. (A completed
    # ingestion lands here too, with the Poison Control line.)
    if medical_kind:
        _set_user_state(uid, crisis_mode=True, stepdown_v=False,
                        banner_v=True, lang_v=safety_lang)
        reply = medical_reply(medical_kind, safety_hebrew,
                              writer_gender)
        _save_assistant_msg(uid, reply)
        return ctx, ("final", {"reply": reply, "crisis": True,
                               "hotline": safety_hotline,
                               "banner": True,
                               "banner_lang": safety_lang,
                               "writer_gender": writer_gender or "",
                               "reply_lang": safety_lang})

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
        # A2: a TRUE crisis sets the latch — and owes exactly one
        # gentle check-in on the following normal turn.
        _set_user_state(uid, crisis_mode=True, stepdown_v=False,
                        banner_v=True, lang_v=banner_lang,
                        checkin_v=True)
        spouse = detect_spouse_loss(msg)
        if spouse:
            # Widow route (Tab ruling): the reply names the loss;
            # the frozen reply serves every other crisis.
            base = spouse_crisis_response(spouse, safety_hebrew)
        else:
            # A5: the Hebrew crisis reply follows the writer's
            # stored gender (masculine = the frozen string).
            base = (crisis_reply_he(writer_gender) if safety_hebrew
                    else FIXED_CRISIS_RESPONSE)
        reply = base + crisis_resource_line(safety_hebrew, base)
        _save_assistant_msg(uid, reply)
        return ctx, ("final", {"reply": reply, "crisis": True,
                               "hotline": safety_hotline, "banner": True,
                               "banner_lang": banner_lang,
                               "writer_gender": writer_gender or "",
                               "reply_lang": safety_lang})

    # Tab r13 item 3: after a FALSE ALARM, the next normal
    # question gets a normal answer. A deterministic intent
    # question (about Solace itself — privacy, voice, identity,
    # availability…) from a latched chat is the person oriented
    # and moving on: the latch and banner clear, and the A8
    # block below answers it with its approved text instead of
    # a crisis-mode model reply. (A fresh risk signal was
    # already handled above and never reaches this point.)
    if in_crisis_mode:
        _lbl = detect_intent_label(msg)
        # "seeking" never unlatches: mid-crisis, "do I need a
        # professional?" stays in the crisis flow, which adds
        # the referral AND the crisis resources (pinned r11).
        if _lbl is not None and _lbl != "seeking":
            _set_user_state(uid, crisis_mode=False,
                            stepdown_v=False, banner_v=False,
                            checkin_v=False, lang_v=safety_lang)
            in_crisis_mode = False
            stepdown = False
            banner = False
            ctx["banner"] = False

    # Tab FINAL order A8: ONE intent classification, one approved
    # deterministic answer per label — identity ("Is this
    # therapy?"), licensed, diagnose, medication (A9), seeking,
    # human, privacy, voice, availability (A12). Deterministic
    # patterns first; the model intent classifier is the backstop
    # for question-shaped messages the patterns don't know. No
    # model reply is ever produced for a labeled turn. (Disabled
    # while crisis mode is latched, as the identity route always
    # was; a grief turn keeps its own flow and its referral.)
    if not in_crisis_mode:
        label = detect_intent_label(msg)
        if label == "seeking" and detect_grief(msg):
            label = None
        if label is None and _question_shaped(msg):
            label = classify_intent_model(msg)
            if label == "seeking" and detect_grief(msg):
                label = None
        if label is not None:
            reply = label_answer(label, safety_hebrew,
                                 writer_gender, profile.get("name"))
            if reply is not None:
                _save_assistant_msg(uid, reply)
                return ctx, ("final", {
                    "reply": reply, "crisis": False,
                    "hotline": hotline, "banner": banner,
                    "banner_lang": banner_lang,
                    "writer_gender": writer_gender or "",
                    "reply_lang": safety_lang})

    # Tab FINAL order A13: a content-free greeting as the first
    # message gets the TEMPLATE reply — name when known, the one
    # fixed self-description, one open question. Never a model
    # improvisation, never a mention of any intake or form.
    if not in_crisis_mode and user_count <= 1 \
            and is_content_free_greeting(msg):
        reply = greeting_reply(profile.get("name"), safety_hebrew,
                               writer_gender)
        _save_assistant_msg(uid, reply)
        return ctx, ("final", {"reply": reply, "crisis": False,
                               "hotline": hotline, "banner": banner,
                               "banner_lang": banner_lang,
                               "writer_gender": writer_gender or "",
                               "reply_lang": safety_lang})

    # Clearly informational word question (and not in crisis mode):
    # answer it, with the gentle check-in — never the crisis script.
    if info_q and not in_crisis_mode:
        ctx["fallback_text"] = INFO_FALLBACK_HE if hebrew else INFO_FALLBACK
        return ctx, ("model", "info", INFO_SYSTEM_PROMPT)

    # Crisis mode latched from an earlier message.
    if in_crisis_mode:
        if detect_clarification(msg) and not risk:
            # Tab FINAL order A2 (supersedes the old two-step
            # step-down): a clarification or denial clears the
            # latch AND the banner, and the reply is the brief
            # fixed line — no check-in question, no crisis text.
            _set_user_state(uid, crisis_mode=False, stepdown_v=False,
                            banner_v=False, checkin_v=False,
                            lang_v=safety_lang)
            reply = (_CLARIFY_HE.get(writer_gender or "",
                                     _CLARIFY_HE["n"])
                     if safety_hebrew else CLARIFY_REPLY)
            _save_assistant_msg(uid, reply)
            return ctx, ("final", {"reply": reply, "crisis": False,
                                   "hotline": safety_hotline,
                                   "banner": False,
                                   "banner_lang": safety_lang,
                                   "writer_gender": writer_gender or "",
                                   "reply_lang": safety_lang})
        elif stepdown or (not _is_latch_fragment(msg)
                          and not ctx["need_professional"]
                          and detect_intent_label(msg) is None):
            # A2: the person moves to a new topic — the latch
            # clears, the banner comes OFF ("Once cleared: banner
            # off, no hotline lines appended"), and this turn is
            # answered normally. If the crisis owed its one gentle
            # check-in, it rides this turn's reply (prepended by
            # code at finish time). A question about help or
            # about Solace itself is part of the crisis
            # conversation, not a new topic — it stays latched.
            _set_user_state(uid, crisis_mode=False, stepdown_v=False,
                            banner_v=False, checkin_v=False,
                            lang_v=safety_lang)
            ctx["banner"] = False
            banner = False
            if profile.get("crisis_checkin"):
                ctx["checkin_line"] = (
                    _CHECKIN_HE.get(writer_gender or "",
                                    _CHECKIN_HE["n"])
                    if safety_hebrew else CHECKIN_LINE)
            ctx["banner_lang"] = safety_lang
            # fall through to the normal flow below
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
        # The writer's own gender (Tab round 4): a woman writing
        # ("אני מתגעגעת") is addressed as a woman — in the fixed
        # opening, in the model's instructions, and in the
        # deterministic cleanup. No signal -> the opening's
        # gender governs, exactly as rounds 2-3 pinned.
        writer_gender = ctx["writer_gender"]
        if hebrew:
            gsys += _grief_gender_note(
                detect_grief_kin_he(msg), writer_gender)
        ctx["grief_hebrew"] = hebrew
        ctx["grief_writer_gender"] = ctx["eff_gender"]
        ctx["grief_opening"] = grief_opening_for(
            hebrew, msg, writer_gender)
        # Tab r13 item 6: a grief FOLLOW-UP never repeats the
        # opening. If the previous assistant reply already
        # opened with a fixed grief opening (this chat has
        # grieved before), this turn carries NO opening — the
        # model continues the conversation — and the finish
        # layers must not resurrect one either.
        _prev_opened = False
        for _m in reversed(messages):
            if _m["role"] == "assistant":
                _prev_opened = any(
                    _m["content"].startswith(_o) for _o in (
                        GRIEF_OPENING, GRIEF_OPENING_HE,
                        GRIEF_OPENING_HE_F, GRIEF_OPENING_HE_N,
                        GRIEF_OPENING_HE_N_F, GRIEF_OPENING_HE_WF,
                        GRIEF_OPENING_HE_WF_F))
                break
        ctx["grief_repeat"] = _prev_opened
        if _prev_opened:
            ctx["grief_opening"] = ""
            gsys += (
                "\n\nNOTE: this conversation has ALREADY "
                "acknowledged the loss with its opening line "
                "in an earlier reply. Do NOT repeat any "
                "condolence opening, do NOT say you are sorry "
                "for the loss again, and do not restart the "
                "conversation as if hearing this for the "
                "first time — simply continue, warmly and "
                "directly, with what the person just said."
            )
        ctx["fallback_text"] = (
            ((ctx["grief_opening"] + " " + GRIEF_FALLBACK_HE)
             if ctx["grief_opening"] else GRIEF_FALLBACK_HE)
            if hebrew
            else ((ctx["grief_opening"] + " " + GRIEF_FALLBACK)
                  if ctx["grief_opening"] else GRIEF_FALLBACK)
        )
        return ctx, ("model", "grief", gsys)

    system = build_system(
        profile, summary,
        has_prior=user_count > 1, returning=returning,
        summary_date=summary_date,
    )
    if med_safe:
        # The model must not re-escalate what the rules have
        # already cleared (Tab round 4, live evidence): its own
        # [CRISIS] marker was the remaining false-alarm channel.
        system += (
            "\n\nNOTE: the person mentioned taking their own "
            "prescribed medication, as directed — ordinary "
            "medication talk, NOT a crisis or overdose signal. "
            "Respond to it normally and warmly; do NOT begin "
            "with [CRISIS], do not safety-interrogate, and do "
            "not treat it as an emergency."
        )
    if stuck_safe:
        # Same rule for "feeling stuck" (Tab round 6): an
        # ordinary feeling, cleared deterministically — the
        # model must not re-escalate it with its own marker.
        system += (
            "\n\nNOTE: the person said they feel stuck in "
            "life — a common, painful but NON-crisis feeling, "
            "NOT a sign of suicidal intent. Respond to it "
            "normally and warmly, and explore what feels "
            "stuck; do NOT begin with [CRISIS], do not "
            "safety-interrogate, and do not treat it as an "
            "emergency."
        )
    if vanish_safe:
        # Same rule for vanish / disappear / done-with-
        # everything (Tab r13): overwhelm, cleared
        # deterministically — the model must not re-escalate.
        system += (
            "\n\nNOTE: the person said they want to "
            "disappear / vanish, or that they are done with "
            "everything — an expression of overwhelm, NOT a "
            "crisis statement (there is no life-or-death "
            "word in their message). Respond normally and "
            "warmly and explore what they want a break "
            "from; do NOT begin with [CRISIS], do not "
            "safety-interrogate, and do not treat it as an "
            "emergency."
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
    # The message that prompted this turn — the invented-worry
    # guard (round 5) judges openers against what the person
    # actually wrote.
    user_text = (ctx["messages"][-1]["content"]
                 if ctx.get("messages") else "")

    if mode == "info":
        if reply.startswith("[CRISIS]"):
            reply = reply[len("[CRISIS]"):].strip()
        reply = scrub_reply(
            reply, ctx.get("eff_gender"), user_text)
        if ctx.get("need_professional"):
            reply = _ensure_referral(reply, safety_hebrew,
                                     ctx.get("writer_gender"))
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
        if ctx.get("grief_repeat"):
            # r13 item 6: a follow-up turn carries no opening —
            # the body's own words are the whole reply (echoes
            # of the fixed opening are still stripped, against
            # the opening this kin would have used).
            opening = ""
            echo_ref = grief_opening_for(
                bool(ctx.get("grief_hebrew")), user_text,
                ctx.get("grief_writer_gender"))
        else:
            opening = ctx.get("grief_opening") or (
                GRIEF_OPENING_HE if ctx.get("grief_hebrew")
                else GRIEF_OPENING)
            echo_ref = opening
        body = _grief_clean_body(
            scrub_field(reply.strip(),
                        ctx.get("grief_writer_gender")), echo_ref,
            ctx.get("grief_writer_gender"),
            repeat=bool(ctx.get("grief_repeat")))
        if not body:
            # The continuation was ONLY a second opening (Tab
            # round 4) — the reply is the fixed opening + the
            # grief fallback line, never the opening alone.
            body = (GRIEF_FALLBACK_HE if ctx.get("grief_hebrew")
                    else GRIEF_FALLBACK)
        reply = (opening + "\n\n" + body) if opening else body
        if ctx.get("need_professional"):
            # The referral guarantee covers EVERY finish path
            # (Tab round 5): the grief branch used to return
            # before _ensure_referral ever ran, so a bereaved
            # person who also asked "do I need a professional?"
            # lost the line.
            reply = _ensure_referral(reply, hebrew, ctx.get("writer_gender"))
        _save_assistant_msg(uid, reply)
        if ctx["user_count"] == 3 or ctx["user_count"] % 5 == 0:
            background.add_task(update_summary, uid)
        return {"reply": reply, "crisis": False, "hotline": hotline,
                "banner": banner, "banner_lang": banner_lang}

    if mode == "crisis":
        if reply.startswith("[CRISIS]"):
            reply = reply[len("[CRISIS]"):].strip()
        reply = scrub_reply(
            reply, ctx.get("eff_gender"), user_text)
        if ctx.get("need_professional"):
            # Same round-5 gap as the grief branch: latched
            # crisis-mode replies never passed an ensure step,
            # so the referral line depended on the model.
            reply = _ensure_referral(reply, safety_hebrew, ctx.get("writer_gender"))
        reply += crisis_resource_line(safety_hebrew, reply)
        _save_assistant_msg(uid, reply)
        return {"reply": reply, "crisis": True,
                "hotline": safety_hotline,
                "banner": True, "banner_lang": safety_lang}

    # normal
    crisis = False
    if reply.startswith("[CRISIS]"):
        reply = reply[len("[CRISIS]"):].strip()
        # A prescribed-medication statement is not-crisis BY
        # RULE (Tab round 4): the model's own marker cannot
        # re-escalate it into a latch, a banner or a hotline
        # footer. The same holds for a feeling-stuck statement
        # (Tab round 6).
        if not ctx["retracted"] and not ctx.get("med_statement") \
                and not ctx.get("stuck_statement") \
                and not ctx.get("vanish_statement"):
            crisis = True
            banner = True
            banner_lang = safety_lang
            _set_user_state(uid, crisis_mode=True, stepdown_v=False,
                            banner_v=True, lang_v=banner_lang)
    reply = scrub_reply(reply, ctx.get("eff_gender"), user_text)
    # Tab FINAL order A9 output guard: dosing content (a number
    # + unit about a substance) replaces the whole reply with
    # the deterministic medication answer.
    if has_dose_advice(reply):
        reply = label_answer("medication", safety_hebrew,
                             ctx.get("writer_gender"))
    # A12 output guard: no Solace-subject availability claim.
    reply = strip_availability_claims(reply)
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
        reply = _ensure_referral(reply, safety_hebrew,
                                 ctx.get("writer_gender"))
    # A2: the ONE gentle check-in owed after a true crisis leads
    # this turn's reply — added by code, exactly once.
    if ctx.get("checkin_line") and not crisis:
        reply = ctx["checkin_line"] + "\n\n" + reply
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


# A17: one open turn per user. A second turn for the same user
# queues behind the first (bounded wait) instead of interleaving
# two replies into one history.
_TURN_LOCKS = {}
_TURN_LOCKS_GUARD = threading.Lock()


def _turn_lock(uid):
    with _TURN_LOCKS_GUARD:
        lk = _TURN_LOCKS.get(uid)
        if lk is None:
            lk = threading.Lock()
            _TURN_LOCKS[uid] = lk
        return lk


def _chat_payload(uid, msg, background):
    lk = _turn_lock((uid or "").strip())
    acquired = lk.acquire(timeout=180)
    try:
        return _chat_payload_inner(uid, msg, background)
    finally:
        if acquired:
            lk.release()


def _chat_payload_inner(uid, msg, background):
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
            canned = ("I'm having trouble connecting right now. "
                      "Please try again in a moment — I'm still "
                      "here.")
            if ctx.get("need_professional"):
                # Even the connection-failure reply honors the
                # referral guarantee (Tab round 5).
                canned = _ensure_referral(
                    canned, ctx.get("safety_hebrew",
                                     ctx["hebrew"]))
            return {"reply": canned, "crisis": False, "hotline": ctx["hotline"], "banner": ctx["banner"], "banner_lang": ctx["banner_lang"]}
        reply = ctx["fallback_text"]
    return _finish_model_turn(ctx, mode, reply, background)


def _safety_lane_turn(uid, msg):
    """Tab r14 item 1: the PRIORITY SAFETY LANE. Tab's re-test
    still saw 18 of 31 crisis/medical runs over 1 s with 9 chats
    running: the r13 fast path ran the FULL _prepare_turn —
    history and summary loads, the whole detector battery (grief,
    intent, retraction, need-professional…), the returning-user
    math — before it reached the safety branches, all of it in
    the event loop, competing with nine streaming turns for the
    same throttled CPU. This lane is the safety check FIRST and
    ONLY: the deterministic safety detectors and their guards,
    one profile read, the user-message insert, and the exact
    branch _prepare_turn would have taken (medical -> emergency
    -> keyword crisis), with byte-identical replies and payload
    shapes. Anything the detectors don't decide outright returns
    None and the turn takes the ordinary path — the lane never
    re-decides a turn the rules leave to _prepare_turn."""
    med_safe = is_prescribed_med_statement(msg)
    medical_kind = None if med_safe else detect_medical_emergency(msg)
    emergency = (detect_overdose(msg) and not med_safe
                 and not is_stuck_in_life_statement(msg))
    kw_risk = detect_crisis_keywords(msg)
    crisis = (kw_risk and not is_never_crisis_idiom(msg)
              and not is_vanish_only_statement(msg))
    if not (medical_kind or emergency or crisis):
        return None
    safety_hebrew = message_is_hebrew(msg)
    safety_lang = "he" if safety_hebrew else "en"
    safety_hotline = "1201" if safety_hebrew else "988"
    with db_lock:
        conn = _conn()
        row = conn.execute(
            "SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            conn.execute("INSERT INTO users (id) VALUES (?)", (uid,))
            conn.commit()
            row = conn.execute(
                "SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        profile = dict(row)
        conn.execute(
            "INSERT INTO messages (user_id, role, content) "
            "VALUES (?,?,?)", (uid, "user", msg))
        conn.commit()
        conn.close()
    # The writer's gender, exactly as _prepare_turn resolves it:
    # this message's own first-person form, else the stored
    # value, else a known first name; stored the moment learned.
    stored_gender = (profile.get("writer_gender") or "") or None
    detected_gender = detect_writer_gender_he(msg)
    if not detected_gender:
        detected_gender = _gender_from_name(profile.get("name"))
    writer_gender = detected_gender or stored_gender
    if detected_gender and detected_gender != stored_gender:
        _set_user_state(uid, gender_v=detected_gender)
    if medical_kind:
        _set_user_state(uid, crisis_mode=True, stepdown_v=False,
                        banner_v=True, lang_v=safety_lang)
        reply = medical_reply(medical_kind, safety_hebrew,
                              writer_gender)
        _save_assistant_msg(uid, reply)
        return {"reply": reply, "crisis": True,
                "hotline": safety_hotline, "banner": True,
                "banner_lang": safety_lang,
                "writer_gender": writer_gender or "",
                "reply_lang": safety_lang}
    if emergency:
        _set_user_state(uid, crisis_mode=True, stepdown_v=False,
                        banner_v=True, lang_v=safety_lang)
        base = (EMERGENCY_RESPONSE_HE if safety_hebrew
                else EMERGENCY_RESPONSE)
        reply = base + crisis_resource_line(safety_hebrew, base)
        _save_assistant_msg(uid, reply)
        return {"reply": reply, "crisis": True,
                "hotline": safety_hotline, "banner": True,
                "banner_lang": safety_lang}
    # Keyword crisis: the fixed reply, latch on, one check-in owed.
    _set_user_state(uid, crisis_mode=True, stepdown_v=False,
                    banner_v=True, lang_v=safety_lang, checkin_v=True)
    spouse = detect_spouse_loss(msg)
    if spouse:
        base = spouse_crisis_response(spouse, safety_hebrew)
    else:
        base = (crisis_reply_he(writer_gender) if safety_hebrew
                else FIXED_CRISIS_RESPONSE)
    reply = base + crisis_resource_line(safety_hebrew, base)
    _save_assistant_msg(uid, reply)
    return {"reply": reply, "crisis": True,
            "hotline": safety_hotline, "banner": True,
            "banner_lang": safety_lang,
            "writer_gender": writer_gender or "",
            "reply_lang": safety_lang}


def _fast_final_turn(uid, msg):
    """Resolve a turn that pure-regex checks decide completely —
    a keyword crisis or a completed overdose — with NO model
    call anywhere in its path, or return None when the turn
    needs the model. The async chat endpoints call this FIRST,
    in the event loop, so the frozen crisis / emergency reply is
    produced the moment the request arrives and never queues
    behind other users' model calls (Tab round 3: with 10 users
    at once the 988 reply took 2–5 s). Tab r14 item 1: the
    deterministic safety branches resolve in the priority lane
    (_safety_lane_turn) — the full _prepare_turn below remains
    only as the fallback for detector-hit turns the lane's
    guards decline (an idiom the keyword layer caught, an
    overdose a stuck-statement disarmed)."""
    lane = _safety_lane_turn(uid, msg)
    if lane is not None:
        return lane
    if is_vanish_only_statement(msg):
        # Tab r13 item 3: vanish-only is cleared by rule — a
        # model turn. Bailing here (before _prepare_turn runs)
        # also keeps the caller from preparing the turn twice.
        return None
    # Tab r13 items 1-2: medical emergencies take this instant
    # route too — the deterministic medical reply must resolve
    # before any model queue, exactly like crisis keywords.
    med_kind = detect_medical_emergency(msg)
    if med_kind and is_prescribed_med_statement(msg):
        med_kind = None
    if not (detect_crisis_keywords(msg) or detect_overdose(msg)
            or med_kind):
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
    lk = _turn_lock((uid or "").strip())
    acquired = lk.acquire(timeout=180)
    try:
        yield from _chat_stream_events_inner(uid, msg, background)
    finally:
        if acquired:
            lk.release()


def _chat_stream_events_inner(uid, msg, background):
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
        # r13 item 6: a follow-up turn (grief_repeat) streams
        # NO opening at all — the `or` fallback must not
        # resurrect one for it.
        opening = ("" if ctx.get("grief_repeat") else
                   ctx.get("grief_opening") or (
                       GRIEF_OPENING_HE if ctx.get("grief_hebrew")
                       else GRIEF_OPENING))
        if opening:
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
                    raw[skip:].lstrip(), opening, grief_started,
                    ctx.get("grief_writer_gender"),
                    repeat=bool(ctx.get("grief_repeat")))
                if grief_frozen or not visible.startswith(
                        emitted_visible):
                    grief_frozen = True
                    continue
            else:
                visible = scrub_prefix(
                    raw[skip:].lstrip(), ctx.get("eff_gender"),
                    msg)
            if len(visible) > len(emitted_visible):
                new = visible[len(emitted_visible):]
                emitted_visible = visible
                yield _sse({"type": "delta", "text": new})
    except Exception:
        failed = True

    if failed and not raw:
        # The model call failed before any text — mirror /api/chat.
        if mode == "normal":
            canned = ("I'm having trouble connecting right now. "
                      "Please try again in a moment — I'm still "
                      "here.")
            if ctx.get("need_professional"):
                # The referral guarantee covers this canned
                # reply too (Tab round 6): round 5 ensured the
                # /api/chat twin but not this one, and a FIRST
                # message — a cold app, a slow first call — is
                # exactly the turn that lands here.
                canned = _ensure_referral(
                    canned, ctx.get("safety_hebrew",
                                     ctx["hebrew"]))
            payload = {"reply": canned, "crisis": False, "hotline": ctx["hotline"], "banner": ctx["banner"], "banner_lang": ctx["banner_lang"]}
            yield _sse({"type": "delta", "text": payload["reply"]})
            yield _sse({"type": "done", **payload})
            return
        raw = ctx["fallback_text"]

    if skip is None:
        skip = len("[CRISIS]") if raw.startswith("[CRISIS]") else 0
    try:
        payload = _finish_model_turn(ctx, mode, raw, background)
    except Exception:
        # The finish step itself failed (Tab round 6): the
        # stream must still END with a done payload — above
        # all on a grief turn, where the client may already
        # be holding the fixed opening and nothing else. The
        # grief reply is opening + fallback here too, NEVER
        # the opening alone; a need-professional turn keeps
        # its referral line on this path as everywhere else.
        _she = ctx.get("safety_hebrew", ctx["hebrew"])
        if mode == "grief":
            body = (GRIEF_FALLBACK_HE if ctx.get("grief_hebrew")
                    else GRIEF_FALLBACK)
            if ctx.get("grief_repeat"):
                # r13 item 6: a follow-up never repeats the
                # opening — the fallback body stands alone.
                payload = {"reply": body,
                           "crisis": False,
                           "hotline": ctx["hotline"],
                           "banner": ctx["banner"],
                           "banner_lang": ctx["banner_lang"]}
            else:
                opening = ctx.get("grief_opening") or (
                    GRIEF_OPENING_HE if ctx.get("grief_hebrew")
                    else GRIEF_OPENING)
                payload = {"reply": opening + "\n\n" + body,
                           "crisis": False,
                           "hotline": ctx["hotline"],
                           "banner": ctx["banner"],
                           "banner_lang": ctx["banner_lang"]}
        elif mode == "crisis":
            rep = ctx["fallback_text"]
            if ctx.get("need_professional"):
                rep = _ensure_referral(rep, _she, ctx.get("writer_gender"))
            rep += crisis_resource_line(_she, rep)
            payload = {"reply": rep, "crisis": True,
                       "hotline": ctx.get("safety_hotline",
                                          ctx["hotline"]),
                       "banner": True,
                       "banner_lang": ctx.get("safety_lang",
                                              ctx["banner_lang"])}
        else:
            rep = ctx["fallback_text"] if mode == "info" else (
                "I'm having trouble connecting right now. "
                "Please try again in a moment — I'm still "
                "here.")
            if ctx.get("need_professional"):
                rep = _ensure_referral(rep, _she, ctx.get("writer_gender"))
            payload = {"reply": rep, "crisis": False,
                       "hotline": ctx["hotline"],
                       "banner": ctx["banner"],
                       "banner_lang": ctx["banner_lang"]}
    final = payload["reply"]
    visible = scrub_prefix(
        raw[skip:].lstrip(), ctx.get("eff_gender"), msg)
    if final.startswith(visible) and len(final) > len(visible):
        yield _sse({"type": "delta", "text": final[len(visible):]})
    elif ctx.get("need_professional"):
        # The on-screen bubble is built from deltas: if the
        # finished text diverged from the streamed prefix, the
        # usual tail-delta cannot be computed — but the
        # referral line must still REACH THE SCREEN (Tab
        # round 6: the anxiety question as a first message
        # lost it in 4 of 5 runs). Emit the appendix itself
        # whenever the final carries it and the screen
        # doesn't.
        _app = _referral_append(ctx.get("safety_hebrew"),
                                ctx.get("writer_gender"))
        if _app in final and _app not in visible:
            yield _sse({"type": "delta", "text": "\n\n" + _app})
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
    # The writer's stored gender steers the Hebrew strings (A5).
    with db_lock:
        _c = _conn()
        _row = _c.execute(
            "SELECT writer_gender FROM users WHERE id=?",
            (uid,)).fetchone()
        _c.close()
    _gender = ((_row["writer_gender"] or "") or None) if _row else None
    if detect_medical_emergency(saved_user_text or ""):
        # Medical emergency first here too (A3) — 911 / מד״א 101
        # leads, not the suicide question.
        base = (medical_reply_he(_gender) if hebrew
                else MEDICAL_REPLY_EN)
    else:
        base = (crisis_reply_he(_gender) if hebrew
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
            "boss": "הבוס שלך", "partner": "בן הזוג שלך",
            "dad": "אבא", "mom": "אמא", "friend": "החבר שלך",
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
    "Delete my data" button.

    Tab r13 item 9: "New chat" mints a NEW user id, so the old
    chat lived on under the old id — deleting only the current
    id left it on the server while the UI said "deleted". The
    client now sends every id this device has ever used
    (body.user_ids); ALL of them are deleted, and the response
    only claims deletion after a verification pass proves zero
    rows remain for every id in every table."""
    uids = []
    for raw in [body.user_id] + list(body.user_ids or []):
        u = (raw or "").strip()
        if u and u not in uids:
            uids.append(u)
    if not uids:
        return {"ok": False, "deleted": False, "remaining": -1}
    tables = ("messages", "summaries", "memory_items",
              "memory_tombstones", "rehearsals",
              "real_talk_followups")
    with db_lock:
        conn = _conn()
        for uid in uids:
            for t in tables:
                conn.execute(
                    "DELETE FROM %s WHERE user_id=?" % t, (uid,))
            conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
        # Verification pass: count what is actually left.
        remaining = 0
        for uid in uids:
            for t in tables:
                remaining += conn.execute(
                    "SELECT COUNT(*) AS c FROM %s WHERE user_id=?"
                    % t, (uid,)).fetchone()["c"]
            remaining += conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE id=?",
                (uid,)).fetchone()["c"]
        conn.close()
    deleted = remaining == 0
    if deleted:
        response.delete_cookie(UID_COOKIE, path="/")
    return {"ok": deleted, "deleted": deleted,
            "remaining": remaining, "users": len(uids)}


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
            "SELECT crisis_mode, crisis_banner, crisis_lang, writer_gender FROM users WHERE id=?", (user_id,)
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
        "writer_gender": (u["writer_gender"] or "") if u else "",
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

"""
Solace — a private AI self-help companion for reflection and support (v2, full-stack).

Backend: FastAPI + SQLite. The AI brain is Anthropic's API, read from the
ANTHROPIC_API_KEY environment variable (set on the hosting provider).
For LOCAL development only, if the env var is missing, calls fall back to
the vault-backed dev CLI — never hardcode or commit any key.
"""
import json
import os
import re
import sqlite3
import subprocess
import threading
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import FileResponse
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
# therapy reply.
FIXED_CRISIS_RESPONSE = (
    "I'm really glad you told me. I want to make sure you're safe right now. "
    "Are you thinking about acting on this tonight? You don't have to go "
    "through this alone — you can call or text 988 (Suicide & Crisis "
    "Lifeline, 24/7) or text HOME to 741741. If you're in immediate danger, "
    "call 911. I'm here with you — do you want to tell me what's going on?"
)

# The same fixed response, fully in Hebrew, for Hebrew conversations
# (re-test fix #8): ERAN 1201 first, then 988 / 741741 / 911.
FIXED_CRISIS_RESPONSE_HE = (
    "אני ממש שמח שסיפרת לי על זה. חשוב לי לוודא שאתה בטוח עכשיו. "
    "האם אתה חושב לפעול על פי המחשבות האלה הלילה? אתה לא צריך לעבור את זה לבד — "
    "בישראל אפשר להתקשר לער״ן, קו הסיוע הנפשי, במספר 1201 (זמין 24/7). "
    "בארה״ב אפשר להתקשר או לשלוח הודעה ל־988 (קו החיים למניעת התאבדות, 24/7), "
    "או לשלוח HOME ל־741741. אם אתה בסכנה מיידית, התקשר ל־911. "
    "אני כאן איתך — אתה רוצה לספר לי מה עובר עליך?"
)

# Gentle check-in sent ONCE when a person walks back a crisis statement
# while crisis mode is latched (re-test fix #2). A retraction no longer
# switches safety mode off instantly: this check-in goes out, crisis
# mode stays latched, and only the NEXT calm, non-risk exchange steps
# therapy back down. No relief emoji, no instant "that's a relief".
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

# System prompt used for follow-up turns while crisis mode is latched:
# safety only — no therapy techniques, no homework, no jargon.
CRISIS_SYSTEM_PROMPT = """You are Solace, in CRISIS MODE with a person who has expressed thoughts of suicide or self-harm. Rules for every reply in this mode:
- Your only job is the person's safety right now. Stay warm, steady, human. Take everything seriously; never panic, never moralize or shame.
- Ask directly how they are doing and whether they are safe right now / tonight. Keep gently checking safety each reply.
- In every reply, remind them: in the US they can call or text 988 (Suicide & Crisis Lifeline, 24/7) or text HOME to 741741; if they are in immediate danger, call 911.
- Match the person's language: reply in the language they are writing in. If they write in Hebrew, your ENTIRE reply must be in Hebrew — not English with a Hebrew line added — and the resources lead with Israel's ERAN emotional-support hotline (1201) first, then 988 / 741741 / 911.
- Do NOT do therapy: no CBT/ACT/DBT techniques, no thought-challenging, no homework or exercises, no analysis of their patterns, no jargon (never say things like "resistance" or "emotional conditioning").
- Short replies (2-4 sentences). One caring question at a time. Stay with them; do not rush to wrap up."""

CLASSIFIER_SYSTEM = (
    "You are a safety classifier for a mental-health support app. Decide "
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

SYSTEM_PROMPT = """You are Solace, a warm and skilled AI self-help companion for reflection and support. You draw deeply on evidence-based approaches — CBT, ACT, DBT, trauma-informed care, attachment theory, grief work, motivational interviewing — but you are NOT a licensed professional, NOT a therapist, and this is NOT therapy.

HARD LIMITS (never break these):
- Never diagnose. Never say or imply "you have depression / anxiety / PTSD / etc." Use non-diagnostic language only: "a working understanding," "it sounds like," "what I'm hearing is."
- Never claim to be a licensed professional or a human being.
- This is not emergency care. If the person is in crisis, follow the crisis protocol below.
- Gently encourage real professional help for serious or persistent struggles.

FIRST-CONVERSATION INTAKE (when this is a new person — no summary of earlier conversations exists):
You have been given their name, current mood, and what's weighing on them (see "About this person"). Do NOT re-ask what you already know.
1. Open warmly. Reflect back what they shared so they feel truly heard.
2. Continue the intake conversationally, ONE question at a time. Across this first conversation, naturally cover: how this has been affecting their daily life and sleep, what support they have around them, how they've been coping so far, and what they'd most want to be different.
3. Conversational, never a checklist or interrogation. Short replies (2-4 sentences), then one question.
4. Toward the end of the intake, share a brief "working understanding" of the person and their situation, gently, and suggest ONE small, concrete first step they could try this week.

ONGOING SESSIONS (when a summary of earlier sessions exists):
- Greet them warmly, show you remember what matters, ask how they've been since last time.
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
        # after therapy steps back down.
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
    Runs BEFORE any therapy model call, on every user message."""
    if detect_crisis_keywords(text):
        return True
    return classify_risk(text)


def detect_retraction(text):
    """True when the user clearly walks back a crisis statement."""
    return bool(RETRACTION_RE.search(text.lower()))


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
            "988 — free, confidential, 24/7."
        )
    return ""


def build_system(profile, summary):
    parts = [SYSTEM_PROMPT]
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
        parts.append("What you remember from earlier sessions:\n" + summary)
    else:
        parts.append(
            "This is a new person — no earlier sessions. Follow the FIRST-SESSION INTAKE."
        )
    return "\n\n---\n\n".join(parts)


def update_summary(user_id):
    """Background job: refresh the rolling clinical-style memory note (cheap model)."""
    try:
        with db_lock:
            conn = _conn()
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 40",
                (user_id,),
            ).fetchall()
            old = conn.execute(
                "SELECT summary FROM summaries WHERE user_id=?", (user_id,)
            ).fetchone()
            conn.close()
        transcript = "\n".join(
            "%s: %s" % (r["role"], r["content"]) for r in reversed(rows)
        )
        prompt = (
            "You are updating a private memory note for an AI support companion. "
            "Read the recent conversation and write (or update) a concise note covering: "
            "presenting concerns, key facts about the person, what seems to be helping, "
            "any practical exercises or homework assigned and whether the person tried them, "
            "and the current focus. Non-diagnostic language only — describe, never label. "
            "Keep it under 200 words. If a previous note exists, merge the new information into it.\n\n"
            "Previous note:\n%s\n\nRecent conversation:\n%s"
            % (old["summary"] if old else "(none)", transcript)
        )
        new_summary = call_anthropic(
            SUMMARY_MODEL,
            "You write brief private memory notes for a support companion.",
            [{"role": "user", "content": prompt}],
            max_tokens=512,
        )
        with db_lock:
            conn = _conn()
            conn.execute(
                "INSERT INTO summaries (user_id, summary, updated_at) VALUES (?,?,datetime('now')) "
                "ON CONFLICT(user_id) DO UPDATE SET summary=excluded.summary, updated_at=datetime('now')",
                (user_id, new_summary),
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


@asynccontextmanager
async def lifespan(app):
    init_db()
    yield


app = FastAPI(title="Solace", lifespan=lifespan)


@app.post("/api/profile")
def save_profile(body: ProfileIn):
    uid = body.user_id.strip() or uuid.uuid4().hex[:16]
    with db_lock:
        conn = _conn()
        conn.execute(
            "INSERT INTO users (id, name, age_range, notes) VALUES (?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, age_range=excluded.age_range, "
            "notes=excluded.notes",
            (uid, body.name.strip(), body.age_range.strip(), body.notes.strip()),
        )
        conn.commit()
        conn.close()
    return {"user_id": uid}


@app.post("/api/chat")
def chat(body: ChatIn, background: BackgroundTasks):
    uid = body.user_id.strip()
    msg = body.message.strip()
    if not uid:
        return {"reply": "Something went wrong starting your session. Please refresh and try again.", "crisis": False, "banner": False, "banner_lang": "en"}
    if not msg:
        return {"reply": "I'm here — what's on your mind?", "crisis": False, "banner": False, "banner_lang": "en"}

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
            "SELECT summary FROM summaries WHERE user_id=?", (uid,)
        ).fetchone()
        summary = srow["summary"] if srow else ""
        hist = conn.execute(
            "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 20",
            (uid,),
        ).fetchall()
        user_count = conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE user_id=? AND role='user'",
            (uid,),
        ).fetchone()["c"]
        conn.close()

    # ---- Pre-model risk check (Solace Fix #1) ----
    # Runs BEFORE any therapy model call, on every user message:
    # keyword layer first, then the classifier fail-safe.
    hebrew = conversation_is_hebrew(hist, msg)
    hotline = "1201" if hebrew else "988"
    kw_risk = detect_crisis_keywords(msg)
    info_q = (not kw_risk) and is_informational_word_question(msg)
    if kw_risk:
        risk = True
    else:
        # The classifier also runs for informational word questions, so
        # anything ambiguous or first-person still escalates; only a
        # clearly informational question the classifier clears takes
        # the gentle path (re-test fix #7).
        risk = classify_risk(msg)
    in_crisis_mode = bool(profile.get("crisis_mode"))
    stepdown = bool(profile.get("crisis_stepdown"))
    banner = bool(profile.get("crisis_banner"))
    # Language of the session's crisis, for the banner. Recorded at
    # latch time; a fresh risk signal in a new language updates it,
    # but follow-ups and retractions keep the crisis's own language.
    banner_lang = profile.get("crisis_lang") or "en"
    # Explicit retraction with no active risk signal is the ONLY exit
    # path from crisis mode — and it is a two-step step-down now
    # (gentle check-in first, one calm exchange after), never an
    # instant exit (re-test fix #2).
    retracted = detect_retraction(msg) and not risk
    messages = [{"role": r["role"], "content": r["content"]} for r in reversed(hist)]

    def _save_assistant(text):
        with db_lock:
            c = _conn()
            c.execute(
                "INSERT INTO messages (user_id, role, content) VALUES (?,?,?)",
                (uid, "assistant", text),
            )
            c.commit()
            c.close()

    def _set_state(crisis_mode=None, stepdown_v=None, banner_v=None, lang_v=None):
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
            c.execute(
                "UPDATE users SET %s WHERE id=?" % ", ".join(sets), vals
            )
            c.commit()
            c.close()

    # Fresh risk signal: fixed human response, no therapy reply, latch
    # on, step-down reset, banner on for the rest of the session. Fully
    # Hebrew for Hebrew conversations (re-test fix #8).
    if risk:
        banner_lang = "he" if hebrew else "en"
        _set_state(crisis_mode=True, stepdown_v=False, banner_v=True,
                   lang_v=banner_lang)
        base = FIXED_CRISIS_RESPONSE_HE if hebrew else FIXED_CRISIS_RESPONSE
        reply = base + crisis_resource_line(hebrew, base)
        _save_assistant(reply)
        return {"reply": reply, "crisis": True, "hotline": hotline, "banner": True, "banner_lang": banner_lang}

    # Clearly informational word question (and not in crisis mode):
    # answer it, with the gentle check-in — never the crisis script.
    if info_q and not in_crisis_mode:
        try:
            reply = call_anthropic(CHAT_MODEL, INFO_SYSTEM_PROMPT, messages)
        except Exception:
            reply = INFO_FALLBACK_HE if hebrew else INFO_FALLBACK
        if reply.startswith("[CRISIS]"):
            reply = reply[len("[CRISIS]"):].strip()
        _save_assistant(reply)
        return {"reply": reply, "crisis": False, "hotline": hotline, "banner": banner, "banner_lang": banner_lang}

    # Crisis mode latched from an earlier message.
    if in_crisis_mode:
        if stepdown:
            # The gentle check-in already went out; this is the next
            # exchange. Risk is False here (a fresh risk signal was
            # handled above and re-latches fully), so a calm, non-risk
            # exchange completes the step-down: therapy resumes, but
            # the banner flag stays on for the rest of the session.
            _set_state(crisis_mode=False, stepdown_v=False)
            in_crisis_mode = False
            # fall through to the normal flow below
        elif retracted:
            # First walk-back: do NOT unlatch. One gentle check-in,
            # no relief tone, still in crisis mode, banner stays.
            _set_state(stepdown_v=True)
            reply = (
                RETRACTION_CHECKIN_RESPONSE_HE if hebrew
                else RETRACTION_CHECKIN_RESPONSE
            )
            reply += crisis_resource_line(hebrew, reply)
            _save_assistant(reply)
            return {"reply": reply, "crisis": True, "hotline": hotline, "banner": True, "banner_lang": banner_lang}
        else:
            system = CRISIS_SYSTEM_PROMPT
            if profile.get("name"):
                system += "\n\nThe person's name is %s." % profile["name"]
            try:
                reply = call_anthropic(CHAT_MODEL, system, messages)
            except Exception:
                reply = (
                    "אני עדיין כאן איתך. אתה בטוח עכשיו? בישראל אפשר "
                    "להתקשר לער״ן במספר 1201 בכל שעה. בארה״ב אפשר "
                    "להתקשר או לשלוח הודעה ל־988, או לשלוח HOME ל־741741."
                    if hebrew else
                    "I'm still here with you. Are you safe right now? "
                    "Please reach out — call or text 988 any time, or text "
                    "HOME to 741741."
                )
            if reply.startswith("[CRISIS]"):
                reply = reply[len("[CRISIS]"):].strip()
            reply += crisis_resource_line(hebrew, reply)
            _save_assistant(reply)
            return {"reply": reply, "crisis": True, "hotline": hotline, "banner": True, "banner_lang": banner_lang}

    crisis = False
    system = build_system(profile, summary)

    try:
        reply = call_anthropic(CHAT_MODEL, system, messages)
    except Exception:
        err = "I'm having trouble connecting right now. Please try again in a moment — I'm still here."
        return {"reply": err, "crisis": False, "hotline": hotline, "banner": banner, "banner_lang": banner_lang}

    if reply.startswith("[CRISIS]"):
        reply = reply[len("[CRISIS]"):].strip()
        if not retracted:
            crisis = True
            banner = True
            banner_lang = "he" if hebrew else "en"
            _set_state(crisis_mode=True, stepdown_v=False, banner_v=True,
                       lang_v=banner_lang)

    if crisis:
        reply += crisis_resource_line(hebrew, reply)

    _save_assistant(reply)

    if user_count % 10 == 0:
        background.add_task(update_summary, user_id)

    return {"reply": reply, "crisis": crisis, "hotline": hotline, "banner": banner, "banner_lang": banner_lang}


@app.post("/api/delete")
def delete_data(body: DeleteIn):
    """Delete ALL data stored for a user_id (re-test fix #3): profile,
    every message, the memory/summary note, and all crisis state.
    Backs the in-app "Delete my data" button."""
    uid = body.user_id.strip()
    if not uid:
        return {"ok": False, "deleted": False}
    with db_lock:
        conn = _conn()
        conn.execute("DELETE FROM messages WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM summaries WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
        conn.close()
    return {"ok": True, "deleted": True}


@app.get("/api/history")
def history(user_id: str):
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
        conn.close()
    return {
        "messages": [dict(r) for r in rows],
        "summary": s["summary"] if s else "",
        "crisis": bool(u["crisis_mode"]) if u else False,
        "banner": bool(u["crisis_banner"]) if u else False,
        "banner_lang": (u["crisis_lang"] or "en") if u else "en",
    }


@app.get("/api/health")
def health():
    return {"ok": True}


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

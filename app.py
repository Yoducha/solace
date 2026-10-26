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
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, BackgroundTasks, Response
from fastapi.responses import FileResponse, StreamingResponse
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


app = FastAPI(title="Solace", lifespan=lifespan)


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
        "banner": banner, "banner_lang": banner_lang,
        "user_count": user_count, "retracted": retracted,
        "returning": returning, "fallback_text": "",
    }

    # Fresh risk signal: fixed human response, no normal reply, latch
    # on, step-down reset, banner on for the rest of the session. Fully
    # Hebrew for Hebrew conversations (re-test fix #8).
    if risk:
        banner_lang = "he" if hebrew else "en"
        _set_user_state(uid, crisis_mode=True, stepdown_v=False,
                        banner_v=True, lang_v=banner_lang)
        base = FIXED_CRISIS_RESPONSE_HE if hebrew else FIXED_CRISIS_RESPONSE
        reply = base + crisis_resource_line(hebrew, base)
        _save_assistant_msg(uid, reply)
        return ctx, ("final", {"reply": reply, "crisis": True,
                               "hotline": hotline, "banner": True,
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
            _set_user_state(uid, crisis_mode=False, stepdown_v=False)
            # fall through to the normal flow below
        elif retracted:
            # First walk-back: do NOT unlatch. One gentle check-in,
            # no relief tone, still in crisis mode, banner stays.
            _set_user_state(uid, stepdown_v=True)
            reply = (
                RETRACTION_CHECKIN_RESPONSE_HE if hebrew
                else RETRACTION_CHECKIN_RESPONSE
            )
            reply += crisis_resource_line(hebrew, reply)
            _save_assistant_msg(uid, reply)
            return ctx, ("final", {"reply": reply, "crisis": True,
                                   "hotline": hotline, "banner": True,
                                   "banner_lang": banner_lang})
        else:
            system = CRISIS_SYSTEM_PROMPT
            if profile.get("name"):
                system += "\n\nThe person's name is %s." % profile["name"]
            ctx["fallback_text"] = (
                "אני עדיין כאן איתך. אתה בטוח עכשיו? בישראל אפשר "
                "להתקשר לער״ן במספר 1201 בכל שעה. בארה״ב אפשר "
                "להתקשר או לשלוח הודעה ל־988, או לשלוח HOME ל־741741."
                if hebrew else
                "I'm still here with you. Are you safe right now? "
                "Please reach out — call or text 988 any time, or text "
                "HOME to 741741."
            )
            return ctx, ("model", "crisis", system)

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

    if mode == "info":
        if reply.startswith("[CRISIS]"):
            reply = reply[len("[CRISIS]"):].strip()
        _save_assistant_msg(uid, reply)
        return {"reply": reply, "crisis": False, "hotline": hotline,
                "banner": banner, "banner_lang": banner_lang}

    if mode == "crisis":
        if reply.startswith("[CRISIS]"):
            reply = reply[len("[CRISIS]"):].strip()
        reply += crisis_resource_line(hebrew, reply)
        _save_assistant_msg(uid, reply)
        return {"reply": reply, "crisis": True, "hotline": hotline,
                "banner": True, "banner_lang": banner_lang}

    # normal
    crisis = False
    if reply.startswith("[CRISIS]"):
        reply = reply[len("[CRISIS]"):].strip()
        if not ctx["retracted"]:
            crisis = True
            banner = True
            banner_lang = "he" if hebrew else "en"
            _set_user_state(uid, crisis_mode=True, stepdown_v=False,
                            banner_v=True, lang_v=banner_lang)
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
    if crisis:
        reply += crisis_resource_line(hebrew, reply)
    _save_assistant_msg(uid, reply)
    # Memory note + reviewable items: build one early (3rd user
    # message) so a returning visitor is actually remembered, then
    # refresh every 5. (It used to update only every 10th message,
    # which short trial conversations never reached.)
    if ctx["user_count"] == 3 or ctx["user_count"] % 5 == 0:
        background.add_task(update_summary, uid)
    return {"reply": reply, "crisis": crisis, "hotline": hotline,
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


@app.post("/api/chat")
def chat(body: ChatIn, background: BackgroundTasks, response: Response):
    if body.user_id.strip():
        _set_uid_cookie(response, body.user_id.strip())
    return _chat_payload(body.user_id, body.message, background)


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

    raw = ""
    skip = None  # None until the [CRISIS]-marker question is settled
    emitted_visible = ""
    failed = False
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
            visible = raw[skip:].lstrip()
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
    visible = raw[skip:].lstrip()
    if final.startswith(visible) and len(final) > len(visible):
        yield _sse({"type": "delta", "text": final[len(visible):]})
    yield _sse({"type": "done", **payload})


@app.post("/api/chat/stream")
def chat_stream(body: ChatIn, background: BackgroundTasks):
    resp = StreamingResponse(
        _chat_stream_events(body.user_id, body.message, background),
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
- DELIVERY (only when the request includes a "DELIVERY (measured)" block): add one more key, "delivery": an array of 2-3 short strings about HOW the user said it — pace, pauses, filler words, steadiness — using ONLY the measured facts in that block. Never invent a number or a quote; where the block gives an example line, quote it exactly. If there is no DELIVERY block in the request, do not include a "delivery" key at all.
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
    if has_hebrew(msg):
        return True
    users = [t.get("content", "") for t in _load_transcript(rec)
             if t.get("role") == "user"]
    if any(has_hebrew(c) for c in users[-3:]):
        return True
    with db_lock:
        c = _conn()
        rows = c.execute(
            "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 20",
            (rec["user_id"],),
        ).fetchall()
        c.close()
    return conversation_is_hebrew(rows, msg)


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
    base = FIXED_CRISIS_RESPONSE_HE if hebrew else FIXED_CRISIS_RESPONSE
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
    # pause_before_ms, longest_pause_ms}). Text only — audio never
    # leaves the device. Empty for typed-only rehearsals.
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
            yield _sse({"type": "delta", "text": payload["reply"]})
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
    emitted = 0
    failed = False
    try:
        for chunk in _anthropic_stream_chunks(system, msgs):
            raw += chunk
            # Emit only the cleaned safe prefix, so a stage
            # direction never flashes as plain dialogue mid-stream.
            safe = _clean_persona_text(_persona_safe_prefix(raw))
            if len(safe) > emitted:
                yield _sse({"type": "delta", "text": safe[emitted:]})
                emitted = len(safe)
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
    reply = _clean_persona_text(raw).strip()
    if len(reply) > emitted:
        yield _sse({"type": "delta", "text": reply[emitted:]})
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


# ------------------------------------------------ Voice Rehearse ----
# Delivery coaching (Tab spec): HOW the user said it, measured in
# code from the spoken turns — never invented by the model. The
# browser's speech recognition produces each spoken line's transcript
# plus timings (speaking span, pause before it, longest mid-line
# pause); the client sends those with the debrief request and the
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
        n = low.count(" %s " % ph) + (1 if low.strip().startswith(ph) else 0)
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
    longest_pause_ms}; each is matched to its transcript line by
    exact (normalized) text, in order, so quotes stay the user's
    exact words from the transcript itself."""
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
        matched.append({
            "line": user_lines[hit],
            "speak_ms": _ms("speak_ms"),
            "pause_before_ms": _ms("pause_before_ms"),
            "longest_pause_ms": _ms("longest_pause_ms"),
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
    if f["long_pauses_before"]:
        lines.append(
            "- Long pauses (3 seconds or more) before starting a "
            "line: %d; the longest was about %s seconds."
            % (f["long_pauses_before"], f["longest_pause_before_s"]))
    if f["mid_pauses"]:
        lines.append(
            "- Long pauses mid-line (2 seconds or more): %d; the "
            "longest was about %s seconds."
            % (f["mid_pauses"], f["longest_mid_pause_s"]))
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
            return {"ok": True, "debrief": json.loads(rec["debrief"])}
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

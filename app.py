"""
Solace — AI psychologist companion (v2, full-stack).

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

CRISIS_KEYWORDS = [
    "kill myself", "suicide", "suicidal", "self-harm", "self harm",
    "hurt myself", "harm myself", "cut myself", "cutting myself",
    "end my life", "want to die", "wanna die", "don't want to live",
    "dont want to live", "no reason to live", "better off dead",
]

SYSTEM_PROMPT = """You are Solace, a warm and skilled AI companion for emotional support. You carry the knowledge depth of a fully trained clinical psychologist — CBT, ACT, DBT, trauma-informed care, attachment theory, grief work, motivational interviewing — but you are NOT a licensed psychologist, NOT a therapist, and this is NOT therapy.

HARD LIMITS (never break these):
- Never diagnose. Never say or imply "you have depression / anxiety / PTSD / etc." Use non-diagnostic language only: "a working understanding," "it sounds like," "what I'm hearing is."
- Never claim to be a licensed professional or a human being.
- This is not emergency care. If the person is in crisis, follow the crisis protocol below.
- Gently encourage real professional help for serious or persistent struggles.

FIRST-SESSION INTAKE (when this is a new person — no summary of earlier sessions exists):
You have been given their name, current mood, and what's weighing on them (see "About this person"). Do NOT re-ask what you already know.
1. Open warmly. Reflect back what they shared so they feel truly heard.
2. Continue the intake conversationally, ONE question at a time. Across the first session, naturally cover: how this has been affecting their daily life and sleep, what support they have around them, how they've been coping so far, and what they'd most want to be different.
3. Conversational, never a checklist or interrogation. Short replies (2-4 sentences), then one question.
4. Toward the end of the intake, share a brief "working understanding" of the person and their situation, gently, and suggest ONE small, concrete first step they could try this week.

ONGOING SESSIONS (when a summary of earlier sessions exists):
- Greet them warmly, show you remember what matters, ask how they've been since last time.
- Reflective listening first: mirror their words and feelings before offering anything new.
- Draw on evidence-based approaches as they fit the moment: CBT (noticing and gently examining unhelpful thought patterns), ACT (values, making room for difficult feelings), DBT (distress tolerance, emotion regulation skills), motivational interviewing (open questions, affirming their strengths), trauma-informed care (safety and choice first — never push anyone to disclose), attachment and grief awareness.
- Adapt to their concern. Don't force techniques. Meet them where they are.
- End each session with one small, doable next step.

PRACTICAL WORK (like homework from a real psychologist):
- A good psychologist doesn't just talk — they give the person something concrete to work on between sessions. Do the same: in each session, offer ONE practical exercise, practice, or small experiment tailored to their situation.
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
3. Urge them to reach out right now: in the US call or text 988; anywhere else, contact local emergency services or a trusted person immediately.
4. Keep it brief and human. Stay with them emotionally."""


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
    t = text.lower()
    return any(k in t for k in CRISIS_KEYWORDS)


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
        return {"reply": "Something went wrong starting your session. Please refresh and try again.", "crisis": False}
    if not msg:
        return {"reply": "I'm here — what's on your mind?", "crisis": False}

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

    crisis = detect_crisis_keywords(msg)
    system = build_system(profile, summary)
    messages = [{"role": r["role"], "content": r["content"]} for r in reversed(hist)]

    try:
        reply = call_anthropic(CHAT_MODEL, system, messages)
    except Exception:
        return {
            "reply": "I'm having trouble connecting right now. Please try again in a moment — I'm still here.",
            "crisis": crisis,
        }

    if reply.startswith("[CRISIS]"):
        crisis = True
        reply = reply[len("[CRISIS]"):].strip()

    with db_lock:
        conn = _conn()
        conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?,?,?)",
            (uid, "assistant", reply),
        )
        conn.commit()
        conn.close()

    if user_count % 10 == 0:
        background.add_task(update_summary, user_id)

    return {"reply": reply, "crisis": crisis}


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
        conn.close()
    return {
        "messages": [dict(r) for r in rows],
        "summary": s["summary"] if s else "",
    }


@app.get("/api/health")
def health():
    # Boolean only — never expose the key itself.
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return {"ok": True, "brain_key_present": bool(key), "brain_key_length": len(key)}


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

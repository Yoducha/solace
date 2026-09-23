# Solace — AI Psychologist Companion (v2)

A mobile-first web app where an AI companion conducts a psychologist-style
first session: it asks how you feel, learns who you are, remembers you across
sessions, and responds with the depth of evidence-based psychology
(CBT, ACT, DBT, trauma-informed, attachment, grief, motivational interviewing).

It is **not** a licensed psychologist, never diagnoses, and shows crisis
guidance (988) when it detects self-harm language.

## How it works

- **Frontend** (`static/index.html`): warm, calm, mobile-first chat UI.
  First visit shows a short intake (name, how you're feeling, what's weighing
  on you), then the conversation begins. A disclaimer banner is always visible;
  a crisis banner with 988 appears if crisis language is detected.
- **Backend** (`app.py`, FastAPI):
  - `POST /api/profile` — create/update a user (name, age range, notes)
  - `POST /api/chat` — send a message; the server loads the user's memory,
    calls the AI with the psychology system prompt, saves the turn, and returns
    the reply plus a `crisis` flag
  - `GET /api/history` — past messages for a user
  - Memory: every 10 turns a background job uses a cheap model to update a
    rolling clinical-style summary (presenting concerns, key facts, progress),
    which is injected into future conversations — so the app remembers the
    person across sessions.
- **Database**: SQLite file (`data/app.db`, created automatically). Zero setup.
  Note: on Render's free tier the disk is ephemeral — conversations reset when
  the service redeploys. That's fine for v2; a permanent database is a later upgrade.
- **AI brain**: Anthropic's API, read from the `ANTHROPIC_API_KEY` environment
  variable. The key is **never** in the code.

## Run locally (for testing)

```bash
cd psychologist-app
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# Local dev only: the app falls back to the vault-backed dev CLI when
# ANTHROPIC_API_KEY is not set. In production the env var is required.
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
# Open http://127.0.0.1:8000
```

## Put it on the internet with Render.com (click-by-click)

**Step 0 — get the code onto GitHub.**
The code lives in `~/workspace/psychologist-app/`. Ask Friend to push it to
your GitHub account — that's a 5-minute job for him, nothing for you to do.

**Step 1 — make a Render account.**
Go to **render.com** and sign up (you can sign up with your GitHub account —
easiest, since the code will be there).

**Step 2 — create the web service.**
1. In the Render dashboard click **New +** → **Web Service**.
2. Choose **Build and deploy from a Git repository** → **Next**.
3. Connect your GitHub if asked, find the `psychologist-app` repository, click **Connect**.

**Step 3 — check the settings.**
Render reads the `render.yaml` file automatically, so most fields fill in
themselves. Make sure:
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `uvicorn app:app --host 0.0.0.0 --port $PORT`

**Step 4 — add your AI key (the important step).**
1. Scroll to **Environment Variables** → click **Add Environment Variable**.
2. Key: `ANTHROPIC_API_KEY`
3. Value: paste your Anthropic API key (the long password starting with
   `sk-ant-` that you created earlier — the same one, nothing new to buy).
4. Click **Save**.

**Step 5 — deploy.**
Click **Create Web Service**. Render builds the app (takes ~2 minutes).
When it says "Live", click the URL at the top — that's your app, on the
internet, usable from any phone.

## Costs

- Render: free tier works (the app sleeps after inactivity and wakes on visit).
- AI brain: roughly **1–10 cents per therapy conversation**, taken from the
  $20 credit already on the Anthropic account. Hundreds of conversations.
- Nothing else costs anything right now.

## Safety notes

- The app states on-screen that it is not a licensed psychologist, doesn't
  diagnose, and isn't for emergencies.
- Crisis language triggers a 988 banner and a caring, non-panicky response.
- This is a v2 prototype, not a medical device. Before real users beyond
  testing, it needs a proper privacy policy, terms, and ideally a legal review.

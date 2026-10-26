"""Voice Mode (Tab spec) — full-screen hands-free voice view.

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_voice_mode.py

Covers:
- Served page: the voice view (orb, status word, bottom bar with
  mute + exit + type field, big "Tap to talk"), the chat composer
  mic, Solace's palette (never black), and the first-use mic line
  wired verbatim behind its once-only gate.
- Client routing: voice-mode turns go through the SAME send paths
  as typed text — sendChat for reflection, rhUserLine with a
  measured vmVoiceTurn for rehearsal — and a crisis payload closes
  voice mode instead of being spoken.
- E2E: a spoken crisis transcript sent exactly as voice mode sends
  it (POST /api/chat/stream, transcript as the message) exits
  pre-model with the frozen response, EN and HE; a calm spoken
  line streams a normal reply.
- The frozen crisis strings are byte-identical (hash pins).
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

# ---------- frozen strings: byte-identical -------------------------
for name, want in (
    ("FIXED_CRISIS_RESPONSE",
     "718f7c98131223578dd68c441f90cad7f72404d9abf3de5952bef39581aa5250"),
    ("FIXED_CRISIS_RESPONSE_HE",
     "d9306c9af9205559b745bfbbf0b9b1ad26df027dc2376cd8325aa569d2c1365d"),
):
    got = hashlib.sha256(getattr(solace, name).encode()).hexdigest()
    if got != want:
        failures.append("FROZEN %s changed: %s" % (name, got))

print("frozen: crisis strings byte-identical")

# ---------- model stubs ----------------------------------------------
stream_calls = {"n": 0}
sync_calls = {"n": 0}


def stub_stream(system, messages):
    stream_calls["n"] += 1
    yield "Stub reply."


def stub_sync(model, system, messages, max_tokens=1024):
    sync_calls["n"] += 1
    return "Stub reply."


solace._anthropic_stream_chunks = stub_stream
solace.call_anthropic = stub_sync

client = TestClient(solace.app)
client.__enter__()

html = client.get("/").text

# ---------- page: the voice view --------------------------------------
for marker in ('id="voice-mode"', 'id="vm-orb"', 'id="vm-status"',
               'id="vm-caption"', 'id="vm-mute"', 'id="vm-exit"',
               'id="vm-input"', 'id="vm-send"', 'id="vm-taptalk"',
               'id="vm-privacy"', 'id="chat-mic"',
               'Tap to talk', 'Listening…', 'Thinking…', 'Speaking…'):
    if marker not in html:
        failures.append("PAGE missing voice-mode marker: %s" % marker)

# Solace colors, never black: the overlay paints from the palette.
vm_css = html[html.find("#voice-mode {"):html.find("#vm-privacy")]
if "var(--bg)" not in vm_css or "black" in vm_css or "#000" in vm_css:
    failures.append("PAGE voice view is not in Solace's palette: %r" % vm_css[:200])
orb_css = html[html.find("#vm-orb {"):html.find("@keyframes vm-breathe")]
if "var(--clay)" not in orb_css or "var(--sage)" not in orb_css:
    failures.append("PAGE orb is not in Solace's colors: %r" % orb_css[:200])

print("page: voice view, orb, status, bottom bar, tap-to-talk")

# ---------- first-use mic line, verbatim -------------------------------
if "solace_vm_seen" not in html:
    failures.append("VM first-use gate missing (solace_vm_seen)")
if "vm-privacy" not in html or "voicePrivacy" not in html:
    failures.append("VM privacy line is not filled from the voicePrivacy strings")
exact = ("Solace never receives or stores your audio — only "
         "the text of what you say.")
if html.count(exact) != 1:
    failures.append("VM exact privacy line count != 1: %d" % html.count(exact))

print("page: first-use mic line verbatim, shown once")

# ---------- client routing ----------------------------------------------
vm_block = html[html.find("// ---- Voice Mode"):html.find("// Returning user")]
for needle in ("await sendChat(text)",          # reflection turns
               "await rhUserLine(text, vt)",    # rehearsal turns
               "vmVoiceTurn(text, sess)",       # measured voice turn
               "payload.crisis", "vmClose('crisis')",  # crisis exits voice mode
               "!rhState",                      # rehearsal crisis exit
               "vmStartListening('barge')",     # barge-in listener
               "vmEnterTapMode",                # graceful degradation
               "spkIdle()"):                    # hands-free loop waits for speech
    if needle not in vm_block:
        failures.append("VM routing missing: %s" % needle)
# The measured turn carries the coaching fields, same as the mic's.
for field in ("speak_ms: rhVoiceDurationMs", "pause_before_ms",
              "longest_pause_ms", "pauses: sess.pauses"):
    if field not in vm_block:
        failures.append("VM voice turn missing field: %s" % field)
# Mic buttons open voice mode in both contexts.
if "vmOpen('chat')" not in html or "vmOpen('rehearse')" not in html:
    failures.append("VM mic entry points missing (chat/rehearse)")

print("client: same send paths, measured turns, crisis exits, barge-in")

# ---------- Tab fresh-user voice fixes (Oct 26) -------------------------
# iPhone: no recognition while Solace speaks + no mic meter; tap-to-
# talk status wording; typing never kills hands-free (cancelled
# sessions); ~50 s silence budget with hands-free coming back.
for needle in ("VM_IOS",                       # iPhone detection
               "spkPending > 0",               # never listen over speech
               "tapDone",                      # 'Tap when done' status
               "sess.cancelled",               # typing pre-empts safely
               "silenceStart",                 # silence budget clock
               "hands-free resumed",           # hands-free comes back
               "vmT().tapTalk"):               # tap idle status string
    if needle not in vm_block:
        failures.append("VM Tab-fix marker missing: %s" % needle)
# The tap-to-talk idle must be set from the tapTalk string, not the
# listening string, in vmAfterSpeech.
tail = vm_block[vm_block.find("async function vmAfterSpeech"):]
tail = tail[:tail.find("async function vmDispatch")]
if "vm.tapMode) { vmSetStatus(vmT().tapTalk)" not in tail:
    failures.append("VM tap idle still says Listening…")
# Rehearse crisis: the stream must carry NO delta (the client speaks
# deltas aloud — a crisis line must never play as audio).
uid = "test-" + uuid.uuid4().hex[:12]
client.post("/api/profile", json={"user_id": uid, "name": "",
                                  "notes": ""})
r = client.post("/api/rehearse/start", json={
    "user_id": uid, "person_type": "boss", "person_name": "Dave",
    "relationship": "my manager", "topic": "a raise",
    "reactions": [], "context": ""}).json()
if not r.get("ok"):
    failures.append("VM rehearse start failed: %r" % r)
else:
    body = client.post("/api/rehearse/stream", json={
        "user_id": uid, "rehearsal_id": r["rehearsal_id"],
        "message": "I want to kill myself"}).text
    if '"type": "delta"' in body or '"type":"delta"' in body:
        failures.append("VM rehearse crisis streamed a delta "
                        "(would be spoken): %r" % body[:200])
    if '"crisis": true' not in body and '"crisis":true' not in body:
        failures.append("VM rehearse crisis lost its payload: %r"
                        % body[:200])

print("client: iPhone guards, tap status, silence budget, "
      "rehearse crisis never streams (never spoken)")

# ---------- E2E: spoken crisis through the voice chat path -------------
# This is byte-for-byte what a spoken line sends from voice mode:
# the transcript as the message of /api/chat/stream (via sendChat).
def new_user():
    uid = "test-" + uuid.uuid4().hex[:12]
    client.post("/api/profile", json={"user_id": uid, "name": "", "notes": ""})
    return uid


uid = new_user()
before = stream_calls["n"] + sync_calls["n"]
body = client.post("/api/chat/stream", json={
    "user_id": uid, "message": "I want to kill myself",
}).text
if '"crisis": true' not in body and '"crisis":true' not in body:
    failures.append("E2E spoken EN crisis did not flag crisis: %r" % body[:300])
if solace.FIXED_CRISIS_RESPONSE[:40] not in body:
    failures.append("E2E spoken EN crisis reply is not the frozen response")
if "988" not in body:
    failures.append("E2E spoken EN crisis has no 988")
if stream_calls["n"] + sync_calls["n"] != before:
    failures.append("E2E spoken EN crisis CALLED the model")

def sse_text(raw):
    """Concatenated delta text + done payload from an SSE body,
    JSON-decoded (the wire format escapes non-ASCII)."""
    out = ""
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            evt = json.loads(line[5:])
        except Exception:
            continue
        if evt.get("type") == "delta":
            out += evt.get("text", "")
        elif evt.get("type") == "done":
            out += "\n" + (evt.get("reply") or "")
    return out


uid = new_user()
before = stream_calls["n"] + sync_calls["n"]
body = client.post("/api/chat/stream", json={
    "user_id": uid, "message": "אני רוצה למות",
}).text
decoded = sse_text(body)
if '"crisis": true' not in body and '"crisis":true' not in body:
    failures.append("E2E spoken HE crisis did not flag crisis: %r" % body[:300])
if solace.FIXED_CRISIS_RESPONSE_HE[:30] not in decoded:
    failures.append("E2E spoken HE crisis reply is not the frozen HE response")
if "1201" not in decoded or "101" not in decoded:
    failures.append("E2E spoken HE crisis missing ERAN 1201 / MDA 101")
if stream_calls["n"] + sync_calls["n"] != before:
    failures.append("E2E spoken HE crisis CALLED the model")

print("e2e: spoken crisis (EN + HE) exits pre-model, frozen reply")

# ---------- E2E: a calm spoken line streams a normal reply ----------------
uid = new_user()
before = stream_calls["n"]
body = client.post("/api/chat/stream", json={
    "user_id": uid, "message": "Work has been draining me lately.",
}).text
if "Stub reply." not in body:
    failures.append("E2E calm spoken line got no streamed reply: %r" % body[:200])
if '"crisis": true' in body or '"crisis":true' in body:
    failures.append("E2E calm spoken line falsely flagged crisis")
if stream_calls["n"] != before + 1:
    failures.append("E2E calm spoken line did not reach the model once")

print("e2e: calm spoken line streams a normal reply")

# ---------- Bug 1: soft orb — fade-in, dim, calm, dark/reduced ---------
vm_css_all = html[html.find("/* ---- Voice Mode"):html.find("/* \"Did this help?\"")]
for needle in ("#voice-mode.shown { opacity: 1; transition: opacity .55s ease; }",
               "@media (prefers-color-scheme: dark)",
               "@media (prefers-reduced-motion: reduce)",
               "animation: none",
               "brightness(.88)"):
    if needle not in vm_css_all:
        failures.append("BUG1 voice CSS missing: %s" % needle)
if "brightness(1.07)" in vm_css_all:
    failures.append("BUG1 old bright breathe keyframe still present")
# The orb loop smooths the level (no per-frame jumps) and renders a
# static orb under Reduce Motion.
for needle in ("VM_REDUCED_MOTION", "* 0.055", "restart listening",
               "window.__vmLog"):
    if needle not in vm_block:
        failures.append("BUG1/2 vm code missing: %s" % needle)
# Fade-in is armed in vmOpen.
if "classList.add('shown')" not in vm_block:
    failures.append("BUG1 vmOpen never adds the .shown fade-in class")

print("bug1: fade-in class, dim orb, smooth loop, dark + reduced motion")

# ---------- Bug 2: Voice Mode never closes on its own -------------------
# vmClose refuses every reason except the user's exit and crisis.
for needle in ("function vmClose(reason)",
               "reason !== 'user' && reason !== 'crisis'",
               "close REFUSED",
               "vmClose('user')",
               "rhExitWasCrisis"):
    if needle not in html:
        failures.append("BUG2 close guard missing: %s" % needle)
# No bare vmClose() / vmClose(<other>) call sites in the voice code.
import re as _re
for m in _re.finditer(r"vmClose\(([^)]*)\)", vm_block):
    if m.group(1).strip() not in ("'user'", "'crisis'", "reason"):
        failures.append("BUG2 unguarded vmClose call: vmClose(%s)" % m.group(1))
# Recognition-end handling restarts or degrades — with logging —
# instead of tearing the view down.
for needle in ("recognition ended, nothing heard",
               "degrade to Tap to talk"):
    if needle not in vm_block:
        failures.append("BUG2 restart logging missing: %s" % needle)

print("bug2: close guard (user/crisis only), logged restarts, rh flag")

if failures:
    print("\nFAILURES (%d):" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL VOICE MODE CHECKS PASSED")

"""First-screen misc (Tab ruling, Oct 26): the vote row hides on
crisis turns, Hebrew phones get a Hebrew intake, the intake is
shorter with exactly 3 example starters, and a lightweight /health
endpoint exists for a keep-alive pinger.

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_intake.py
"""
import os
import sys

os.environ["CRISIS_CLASSIFIER"] = "off"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as solace  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

failures = []
client = TestClient(solace.app)
client.__enter__()
html = client.get("/").text

# ---------- health endpoints ------------------------------------------
for path in ("/health", "/api/health"):
    r = client.get(path)
    if r.status_code != 200 or r.json().get("ok") is not True:
        failures.append("HEALTH %s not a fast 200 ok: %s"
                        % (path, r.status_code))

# ---------- exactly 3 starters ------------------------------------------
if html.count('class="chip starter-chip"') != 3:
    failures.append("STARTERS count != 3: %d"
                    % html.count('class="chip starter-chip"'))
for s in ("I can't sleep — my mind won't stop racing.",
          "Work has been overwhelming lately.",
          "I had a fight with someone I love."):
    if s not in html:
        failures.append("STARTERS missing: %r" % s)
if "id=\"starters\"" not in html or "id=\"starters-label\"" not in html:
    failures.append("STARTERS block ids missing")

# ---------- shorter intake ----------------------------------------------
if "Solace remembers key themes so you can pick up" in html:
    failures.append("INTAKE still carries the long audience copy")
if 'id="lead-line"' not in html:
    failures.append("INTAKE lead id missing")

# ---------- Hebrew intake -------------------------------------------------
for marker in ("UI_HE", "BTXT", "applyIntakeHe".replace("applyIntakeHe", "UI_HE"),
               "להתחיל התבוננות", "מה הכי מכביד עליך?",
               "לא בטוח מאיפה להתחיל?", "id=\"tagline\"",
               "id=\"audience-line\"", "id=\"mood-label\"",
               "id=\"concern-label\"", "id=\"tiny-line\""):
    if marker not in html:
        failures.append("HE INTAKE marker missing: %s" % marker)
# The localized begin-button states route through BTXT.
if html.count("BTXT.begin") < 3:
    failures.append("HE INTAKE button states not localized: %d"
                    % html.count("BTXT.begin"))

# ---------- vote row hides on crisis --------------------------------------
if "function hideChatVote()" not in html:
    failures.append("VOTE hideChatVote missing")
anchor = html.find("if (payload.crisis) hideChatVote();")
if anchor < 0:
    failures.append("VOTE chat crisis payload does not hide the row")
rh = html[html.find("function rhCrisisExit"):]
if "hideChatVote();" not in rh[:400]:
    failures.append("VOTE rehearse crisis exit does not hide the row")

if failures:
    print("\nFAILURES (%d):" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("health ok, 3 starters, shorter intake, Hebrew intake wired, "
      "vote hides on crisis")
print("\nALL INTAKE CHECKS PASSED")

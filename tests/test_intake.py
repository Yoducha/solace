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

# Tab round 2: the intake's explanatory prose (tagline, privacy
# line, audience, lead, tiny print) must total <= ~70 words, with
# the approved privacy sentence verbatim and "Not therapy" once.
import re as _re


def _elem_text(html_src, elem_id):
    m = _re.search(
        r'id="%s"[^>]*>(.*?)</p>' % elem_id, html_src, _re.S)
    if not m:
        return ""
    return _re.sub(r"<[^>]+>", " ", m.group(1))


_prose = " ".join(
    _elem_text(html, i) for i in (
        "tagline", "privacy-line", "audience-line", "lead-line",
        "tiny-line"))
_prose_words = len(_prose.split())
if _prose_words > 70:
    failures.append("INTAKE prose is %d words (want <= 70)"
                    % _prose_words)
if ("Your chats aren't shared with other people or used to "
        "advertise to you. See the privacy page for details."
        not in _elem_text(html, "lead-line")):
    failures.append("INTAKE lead lost the approved privacy sentence")
_welcome = html[html.find('<section id="welcome">'):
                html.find('</section>')]
if _welcome.count("Not therapy") != 1:
    failures.append("INTAKE 'Not therapy' count != 1: %d"
                    % _welcome.count("Not therapy"))

# ---------- no availability phrasing anywhere on the page ----------------
# Tab round 4 (his "r10" test): "Heavy mind? Day or night, no
# judgment." (HE: "ביום ובלילה") is an availability claim by
# another name — the intake line is deleted, in both languages.
for bad in ("Day or night", "ביום ובלילה", "Heavy mind"):
    if bad in html:
        failures.append("INTAKE still carries availability copy: %r"
                        % bad)
if 'id="audience-line"' in html:
    failures.append("INTAKE audience line still present")

# ---------- Hebrew intake -------------------------------------------------
# FINAL order A16: the old UI_HE block is superseded by the ONE
# UI_STRINGS table — the markers name the new machinery.
for marker in ("UI_STRINGS", "BTXT", "applyUIStrings",
               "להתחיל התבוננות", "מה הכי מכביד עכשיו?",
               "לא בטוח מאיפה להתחיל?", "id=\"tagline\"",
               "id=\"mood-label\"",
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

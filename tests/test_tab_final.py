"""Tab FINAL order — build checks (A11, A15, A16, A19).

A11: ONE banned-word filter covers every user-visible string,
including the shipped pages themselves — this test greps the
templates/pages and fails on a hit. The only allowed forms are
the exact disclaimers "Not therapy" / "לא טיפול".
A15: no "/" between Hebrew letters anywhere in the pages.
A16: the ONE UI string table exists with matching EN/HE keys;
Terms/Privacy carry full Hebrew versions and a crisis section
with 988 AND ער״ן 1201 AND מד״א 101; the Hebrew SOS pill names
1201/101, not only 988.
A19: the 44px tap-target rules are in the stylesheet.
(A20 — the contact address — is intentionally NOT asserted:
it is blocked on Yishay choosing a dedicated Solace address.)
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app as solace  # noqa: E402

failures = []
BASE = os.path.join(os.path.dirname(__file__), "..", "static")
PAGES = {name: open(os.path.join(BASE, name)).read()
         for name in ("index.html", "terms.html", "privacy.html")}

# ---- A11: banned-word grep over the raw shipped files ---------
for name, raw in PAGES.items():
    txt = raw.replace("לא טיפול", "").replace("Not therapy", "")
    for rx in solace._BANNED_CHECK_RES + [solace._THERAP_RE]:
        for m in rx.finditer(txt):
            failures.append("A11 %s carries banned %r: ...%s..."
                            % (name, m.group(0),
                               txt[max(0, m.start() - 30):m.start() + 30]))
    for w in solace._BANNED_HE:
        if w in txt:
            failures.append("A11 %s carries banned HE word %r"
                            % (name, w))
    # CBT / ACT / DBT / patient never appear at all.
    for w in ("CBT", "ACT", "DBT", "patient"):
        if re.search(r"\b%s\b" % w, raw):
            failures.append("A11 %s carries %r" % (name, w))

# ---- A15: no slash between Hebrew letters ----------------------
for name, raw in PAGES.items():
    for m in re.finditer(r"[\u0590-\u05FF]/[\u0590-\u05FF]", raw):
        failures.append("A15 %s has a Hebrew slash pair: %r"
                        % (name, raw[m.start() - 12:m.start() + 13]))

# ---- A16: one string table, both languages ---------------------
idx = PAGES["index.html"]
if "const UI_STRINGS = {" not in idx:
    failures.append("A16 UI_STRINGS table missing")
for key in ("sosFull", "tagline", "memTitle", "welcomeBack",
            "confirmDelete", "tinyLine", "moodLabel"):
    if idx.count(key + ":") < 2:
        failures.append("A16 key %r not present in both languages"
                        % key)
if "document.documentElement.lang = UILang" not in idx \
        or "document.documentElement.dir" not in idx:
    failures.append("A16 html lang/dir are not set from the table")
if "ער״ן 1201" not in idx or "מד״א 101" not in idx:
    failures.append("A16 Hebrew SOS pill must name 1201 and 101")
if "VOICE_PRIVACY_HE" not in idx \
        or "סולאס אף פעם לא מקבל או שומר את האודיו שלך" not in idx:
    failures.append("A16/A5 voice-privacy table (pinned text) "
                    "missing")
for name in ("terms.html", "privacy.html"):
    raw = PAGES[name]
    if 'id="page-he"' not in raw or 'lang="he"' not in raw:
        failures.append("A16 %s has no full Hebrew version" % name)
    for num in ("988", "1201", "101"):
        if num not in raw:
            failures.append("A16 %s crisis section missing %s"
                            % (name, num))
# The approved privacy sentence survives verbatim, both languages.
if "Your chats aren't shared with other people or used to " \
        "advertise to you. See the privacy page for details." \
        not in PAGES["privacy.html"]:
    failures.append("A16 privacy page lost the approved EN line")
if "השיחות שלך לא משותפות עם אנשים אחרים ולא משמשות כדי " \
        "לפרסם לך" not in PAGES["privacy.html"]:
    failures.append("A16 privacy page lost the approved HE line")

# ---- A19: tap targets ------------------------------------------
for sel in (".hdr-btn { min-height: 44px", ".chip { min-height: 44px",
            ".mem-item button { min-width: 44px",
            ".msg .speak { min-width: 44px",
            "#help-vote button { min-width: 44px",
            "#composer button { min-height: 44px"):
    if sel not in idx:
        failures.append("A19 tap-target rule missing: %s" % sel)

# ---- A17: the client busy state --------------------------------
if "function setChatBusy" not in idx or "send.disabled = busy" not in idx:
    failures.append("A17 Send/mic busy state missing")

# ---- A18: welcome + rehearse carry the data controls -----------
if 'id="welcome-data-row"' not in idx:
    failures.append("A18 welcome-screen data row missing")
if idx.count('data-act="delete"') < 2 or idx.count('data-act="memory"') < 2:
    failures.append("A18 memory/delete not reachable from "
                    "welcome AND rehearse")

if failures:
    print("FAILURES (%d):" % len(failures))
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("build checks: banned-word grep clean (A11), no Hebrew "
      "slash pairs (A15), one EN+HE string table + full HE pages "
      "(A16), 44px targets (A19), busy state (A17), data "
      "controls everywhere (A18)")
print("\nALL FINAL-ORDER BUILD CHECKS PASSED")

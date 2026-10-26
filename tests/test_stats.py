"""Anonymous usage stats (Tab spec) — checks.

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_stats.py

The hard rule under test: counts only. Seeded fake visitors across
fake dates must produce exact new-user / day+7 / day+30 numbers;
the stats DB must contain no raw id and no content-shaped field;
votes store aggregates only; /stats is 404 without the key.
"""
import hashlib
import os
import sqlite3
import sys
import tempfile
import uuid

os.environ["CRISIS_CLASSIFIER"] = "off"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as solace  # noqa: E402

failures = []

# Hermetic stats DB in /tmp, patched in before any stats call.
_tmp = tempfile.mkdtemp(prefix="solace-stats-")
solace.STATS_DB_PATH = os.path.join(_tmp, "stats.db")

D0 = "2026-09-20"    # cohort day (35 days before the run date)
D7 = "2026-09-27"    # D0 + 7
D30 = "2026-10-20"   # D0 + 30
A, B, C = ("uid-" + uuid.uuid4().hex[:10] for _ in range(3))

for uid in (A, B, C):
    solace.stats_touch(uid, day=D0)
for uid in (A, B):
    solace.stats_touch(uid, day=D7)
solace.stats_touch(A, day=D30)
solace.stats_feature(A, "rehearse", day=D0)
solace.stats_feature(B, "rehearse", day=D0)
solace.stats_feature(A, "voice", day=D0)

rep = solace._stats_report()
new = {r["day"]: r["n"] for r in rep["new_by_day"]}
if new.get(D0) != 3:
    failures.append("REPORT new users on %s != 3: %r" % (D0, new))
cohort = next((c for c in rep["cohorts"] if c["day"] == D0), None)
if not cohort or cohort["size"] != 3:
    failures.append("REPORT cohort missing/wrong: %r" % cohort)
else:
    if cohort["d7"] != 2:
        failures.append("REPORT day+7 returns != 2: %r" % cohort)
    if cohort["d30"] != 1:
        failures.append("REPORT day+30 returns != 1: %r" % cohort)
if rep["rehearse_users"] != 2 or rep["voice_users"] != 1:
    failures.append("REPORT feature users wrong: %r" % rep)

print("report: new/day+7/day+30 + feature counts exact")

# ---------- the store itself: hashed ids, no content fields --------
conn = sqlite3.connect(solace.STATS_DB_PATH)
salt = conn.execute("SELECT v FROM stat_meta WHERE k='salt'").fetchone()[0]
expected_hid = hashlib.sha256((salt + ":" + A).encode()).hexdigest()
hids = [r[0] for r in conn.execute("SELECT hid FROM stat_visitors")]
if expected_hid not in hids:
    failures.append("STORE visitor A not stored under the salted hash")
if A in hids or any(uid in hids for uid in (B, C)):
    failures.append("STORE a raw visitor id was stored")
dump = []
cols = {}
for (tname,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"):
    cols[tname] = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % tname)]
    for row in conn.execute("SELECT * FROM %s" % tname):
        dump.append(" ".join(str(x) for x in row))
blob = "\n".join(dump)
for uid in (A, B, C):
    if uid in blob:
        failures.append("STORE raw uid %s appears in the stats DB" % uid)
ALLOWED_COLS = {"k", "v", "hid", "first_seen", "day", "feature",
                "surface", "vote", "n"}
for tname, names in cols.items():
    extra = set(names) - ALLOWED_COLS
    if extra:
        failures.append("STORE table %s has non-count columns: %r" % (tname, extra))
conn.close()

print("store: salted one-way hashes only; allowlisted columns; no raw ids")

# ---------- endpoints ----------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(solace.app)
client.__enter__()

r = client.post("/api/stats/event", json={"user_id": "walk-in-uid",
                                          "event": "visit"})
if r.status_code != 200 or r.json().get("ok") is not True:
    failures.append("EVENT endpoint failed: %r" % r.status_code)
conn = sqlite3.connect(solace.STATS_DB_PATH)
raw = [row for row in conn.execute("SELECT * FROM stat_visitors")]
if any("walk-in-uid" in str(row) for row in raw):
    failures.append("EVENT stored the raw user id")
conn.close()

for payload in ({"surface": "chat", "vote": 1},
                {"surface": "chat", "vote": 1},
                {"surface": "chat", "vote": -1,
                 "user_id": "must-not-be-stored"},
                {"surface": "rehearse", "vote": 1},
                {"surface": "bogus", "vote": 1},
                {"surface": "chat", "vote": 5}):
    r = client.post("/api/stats/vote", json=payload)
    if r.status_code != 200:
        failures.append("VOTE endpoint failed on %r" % payload)
rep = solace._stats_report()
v = rep["votes"]
if v.get("chat", {}).get(1) != 2 or v.get("chat", {}).get(-1) != 1:
    failures.append("VOTE chat counts wrong: %r" % v)
if v.get("rehearse", {}).get(1) != 1:
    failures.append("VOTE rehearse counts wrong: %r" % v)
if "bogus" in v:
    failures.append("VOTE bogus surface counted")
conn = sqlite3.connect(solace.STATS_DB_PATH)
blob = "\n".join(" ".join(str(x) for x in row)
                 for t in ("stat_meta", "stat_visitors", "stat_activity",
                           "stat_features", "stat_votes")
                 for row in conn.execute("SELECT * FROM %s" % t))
if "must-not-be-stored" in blob:
    failures.append("VOTE stored an identity field")
conn.close()

print("endpoints: visit hashed; votes aggregate-only, surfaces validated")

# ---------- /stats page: locked without key, exact with key ---------
os.environ.pop("STATS_KEY", None)
if client.get("/stats").status_code != 404:
    failures.append("PAGE /stats open while STATS_KEY unset")
if client.get("/stats", params={"key": "x"}).status_code != 404:
    failures.append("PAGE /stats open with a key while env unset")
os.environ["STATS_KEY"] = "test-key-123"
if client.get("/stats").status_code != 404:
    failures.append("PAGE /stats open without the key")
if client.get("/stats", params={"key": "wrong"}).status_code != 404:
    failures.append("PAGE /stats open with the wrong key")
r = client.get("/stats", params={"key": "test-key-123"})
if r.status_code != 200:
    failures.append("PAGE /stats locked with the right key")
else:
    body = r.text
    for marker in ("<td>%s</td><td>3</td>" % D0,
                   "Rehearse users", "Voice Rehearse users",
                   "<td>2</td><td>1</td>"):
        if marker not in body:
            failures.append("PAGE missing expected content: %s" % marker)
    for banned in ("therapy", "therapist", "psychologist", "counselor",
                   "treatment", "diagnose", "heal", "cure"):
        if banned in body.lower():
            failures.append("PAGE stats copy contains banned word: %s" % banned)

print("page: 404 locked (unset/missing/wrong), exact numbers with key")

# ---------- privacy page carries the line, EN + HE ------------------
priv = client.get("/privacy").text
if "Anonymous usage counts" not in priv or "never include anything you wrote" not in priv:
    failures.append("PRIVACY English counts line missing")
if "ספירת שימוש אנונימית" not in priv:
    failures.append("PRIVACY Hebrew counts line missing")

print("privacy: anonymous-counts line present in English and Hebrew")

# ---------- the app page carries the vote wiring --------------------
html = client.get("/").text
for marker in ('id="help-vote"', '/api/stats/vote', '/api/stats/event',
               'solace_voted_rh', 'solace_voted_chat', 'statVisit'):
    if marker not in html:
        failures.append("APP PAGE missing stats marker: %s" % marker)

print("app page: vote rows + visit ping wired")

if failures:
    print("\nFAILURES (%d):" % len(failures))
    for x in failures:
        print(" -", x)
    sys.exit(1)
print("\nALL STATS CHECKS PASSED")

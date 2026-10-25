"""Tab fix — a deleted memory item must never come back, in any
wording, through a memory rewrite (incl. the post-rehearsal one).

Run: CRISIS_CLASSIFIER=off .venv/bin/python tests/test_memory.py
(The model call is stubbed; the summary stub deliberately outputs
Tab's reworded resurrection text.)
"""
import os
import sys
import uuid

os.environ["CRISIS_CLASSIFIER"] = "off"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as solace  # noqa: E402

failures = []

# ---------- unit: the semantic tombstone matcher ----------
DELETED = "work projects accumulating; feeling perpetually behind"
REWORDED = "Manager continuously adds projects; feels perpetually behind even on weekends"
tomb = [ (solace._item_tokens(DELETED), "2026-10-25 12:00:00") ]
old_msgs = [("2026-10-25 11:00:00",
             solace._item_tokens("work is piling up and I feel behind"))]

if not solace._deleted_and_not_restated(REWORDED, tomb, old_msgs):
    failures.append("UNIT reworded deleted theme NOT blocked (Tab repro)")
if not solace._deleted_and_not_restated(DELETED, tomb, old_msgs):
    failures.append("UNIT exact deleted theme NOT blocked")
if solace._deleted_and_not_restated("Sleep has been poor lately", tomb, old_msgs):
    failures.append("UNIT unrelated theme wrongly blocked")
if solace._deleted_and_not_restated("Feeling anxious about money", tomb, old_msgs):
    failures.append("UNIT different theme wrongly blocked")
if solace._deleted_and_not_restated("Feeling anxious about sleep", tomb, old_msgs):
    failures.append("UNIT different theme (2 shared words) wrongly blocked")

# The person re-raises it themselves AFTER deleting -> allowed back.
restated = [("2026-10-25 13:00:00",
             solace._item_tokens(
                 "Work keeps piling up — I feel perpetually behind on all my projects"))]
if solace._deleted_and_not_restated(REWORDED, tomb, restated):
    failures.append("UNIT user restatement after deletion should be allowed")

print("unit: semantic tombstone matcher (reword blocked, exact blocked, "
      "unrelated allowed, restatement allowed)")

# ---------- end-to-end: add -> delete -> rewrite must not resurrect ----
from fastapi.testclient import TestClient  # noqa: E402

summary_out = {"text": ""}


def stub_anthropic(model, system, messages, max_tokens=1024):
    if model == solace.SUMMARY_MODEL:
        return summary_out["text"]
    return "Stub reply."


solace.call_anthropic = stub_anthropic
uid = "test-" + uuid.uuid4().hex[:12]
client = TestClient(solace.app)
client.__enter__()

for m in ("Work is piling up and I feel behind",
          "My manager keeps handing me more projects",
          "I feel perpetually behind even on weekends"):
    client.post("/api/chat", json={"user_id": uid, "message": m})

summary_out["text"] = (
    "Note.\nITEMS: [{\"kind\": \"theme\", \"text\": \"" + DELETED + "\"}]"
)
solace.update_summary(uid)
mem = client.get("/api/memory", params={"user_id": uid}).json()
texts = [i["text"] for i in mem["items"]]
if DELETED not in texts:
    failures.append("E2E setup: theme never landed in memory: %r" % texts)

item_id = mem["items"][0]["id"]
r = client.post("/api/memory/delete-item",
                json={"user_id": uid, "item_id": item_id}).json()
if r["items"]:
    failures.append("E2E delete did not remove the item: %r" % r)

# The post-rehearsal rewrite: the model re-adds the SAME theme reworded
# (Tab's exact resurrection text), plus one genuinely new theme.
summary_out["text"] = (
    "Note.\nITEMS: ["
    "{\"kind\": \"theme\", \"text\": \"" + REWORDED + "\"}, "
    "{\"kind\": \"theme\", \"text\": \"Sleep has been poor lately\"}]"
)
solace.update_summary(uid)
mem = client.get("/api/memory", params={"user_id": uid}).json()
texts = [i["text"] for i in mem["items"]]
if any("perpetually behind" in t for t in texts):
    failures.append("E2E RESURRECTION: deleted theme came back reworded: %r" % texts)
if "Sleep has been poor lately" not in texts:
    failures.append("E2E new unrelated theme missing after rewrite: %r" % texts)

print("end-to-end: theme added -> deleted -> rewrite with reworded text "
      "-> stays deleted; new theme lands")

if failures:
    print("\nFAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL TESTS PASSED")

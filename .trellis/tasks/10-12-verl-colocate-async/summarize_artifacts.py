"""Assert recorded browser payloads, SDK test counts and report reproducible evidence."""
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

work = Path(sys.argv[1]).resolve()
events = [json.loads(line) for line in (work / "requests.jsonl").read_text().splitlines()]
posts = [e for e in events if e["method"] == "POST" and e["path"].startswith("/api/")]
assert all(e["status"] in (200, 503) for e in posts)
creates = [e for e in posts if e["path"] == "/api/runs" and e["status"] == 200]
assert len(creates) == 3
# Exactly four explicit confirmations: sync, async failure, async retry, Chinese async.
# The three Cancel actions must add no POST at all, including rejected creates.
assert sum(e["path"] == "/api/runs" for e in posts) == 4
assert [e["body"]["training_mode"] for e in creates] == ["sync", "colocate_async", "colocate_async"]
assert creates[0]["body"]["compute"]["max_hours"] is None
last = None
for event in posts:
    body = event["body"]
    if body["training_mode"] == "colocate_async":
        assert body["compute"]["max_hours"] * 3600 >= 1
    if event["path"] == "/api/runs/preview" and event["status"] == 200:
        last = body
    if event["path"] == "/api/runs":
        assert body["confirm_cost"] is True
        assert {k: v for k, v in body.items() if k != "confirm_cost"} == last
failed = [e for e in posts if e["status"] == 503]
assert len(failed) == 2
for event in failed:
    assert event["body"]["training_mode"] == "colocate_async"
    assert event["body"]["agent_runtime_id"] == "rt-office"
    assert event["body"]["params"] == {"train_batch_size": 8, "rollout_n": 4}
    assert any(e["status"] == 200 and e["path"] == event["path"] and e["body"] == event["body"] for e in posts)
suite = ET.parse(work / "sdk.xml").getroot().find("testsuite")
assert suite is not None
assert (suite.get("tests"), suite.get("skipped"), suite.get("failures"), suite.get("errors")) == ("138", "0", "0", "0")
compose = json.loads((work / "compose-results.json").read_text())
assert len(compose["matrix"]) == 8 and all(r["result"] == "PASS" for r in compose["matrix"])
summary = {
    "SDK": dict(suite.attrib), "Hydra": compose,
    "successful_previews": sum(e["path"] == "/api/runs/preview" and e["status"] == 200 for e in posts),
    "successful_creates": [e["body"] for e in creates], "expected_injected_failures": failed,
    "checks": json.loads((work / "browser-checks.json").read_text()),
    "screenshots": [str(p) for p in sorted(work.glob("*.png"))],
    "layout_caveat": "Existing global overflow: model wizard scrollWidth en=562/zh=439 at innerWidth=390. Not fixed.",
}
(work / "evidence-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
print("PASS payloads: 3 confirmed creates match latest preview exactly; both injected failures retry unchanged")
print("PASS fixed framework: 138 SDK tests, 0 skipped; 8/8 actual compositions")
print(work / "evidence-summary.json")

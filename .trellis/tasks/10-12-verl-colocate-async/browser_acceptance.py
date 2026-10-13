"""Repeatable bilingual/390px acceptance through agent-browser ONLY.
Requires browser_services.py running and renders.json; supports nonempty mock state.
python3 browser_acceptance.py WORK
Uses real keyboard events for number inputs (CLI fill did not update React here).
"""
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.request import Request, urlopen

TASK = Path(__file__).resolve().parent
ROOT = TASK.parents[2]
WORK = Path(sys.argv[1]).resolve()
BASE = "http://127.0.0.1:5187/runs"
wizard_only = "--wizard-only" in sys.argv
checks = json.loads((WORK / "browser-checks.json").read_text()) if wizard_only else []


def ab(*args):
    result = subprocess.run([sys.executable, str(TASK / "browser_cli.py"), str(WORK), *args],
                            text=True, capture_output=True, timeout=50)
    if result.returncode:
        raise AssertionError((args, result.stdout, result.stderr))
    return result.stdout


def qa(body=None):
    req = Request("http://127.0.0.1:18187/__qa", data=json.dumps(body).encode() if body else None,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=5) as response:
        return json.load(response)


def snapshot():
    return ab("snapshot")


def click_button(text):
    snap = snapshot()
    pattern = r'- button "' + re.escape(text) + r'"[^\n]*ref=(e\d+)'
    match = re.search(pattern, snap)
    assert match, (text, snap)
    ab("click", "@" + match[1])


def next_step():
    ab("focus", '[data-testid="tp-run-next"]')
    ab("press", "Enter")


def number(value):
    ab("focus", '[data-testid="tp-run-max-hours"]')
    ab("press", "Control+a")
    ab("press", "Backspace")
    if value:
        ab("keyboard", "type", value)
    ab("press", "Tab")


def assert_js(expression):
    result = ab("eval", f"Boolean({expression})").strip()
    assert result == "true", (expression, result)


def record(name, **extra):
    checks.append({"check": name, "result": "PASS", **extra})
    (WORK / "browser-checks.json").write_text(json.dumps(checks, indent=2, ensure_ascii=False))
    print(name, "PASS", flush=True)


def screenshot(name):
    print(ab("screenshot", str(WORK / name)).strip(), flush=True)


# Fresh service starts at about:blank; enter only the owned loopback frontend.
ab("open", f"{BASE}?view=detail&id=legacy&tab=config")
ab("wait", "--fn", "!!document.querySelector('.v2-lang')")
# Legacy missing-key and explicitly invalid stored values in both locales; reload is real.
for lang, toggle in (() if wizard_only else (("en", "EN"), ("zh-CN", "中文"))):
    click_button(toggle)
    strings = json.loads((ROOT / f"frontend/src/locales/{lang}/common.json").read_text())["runs"]
    for run_id, key in (("legacy", "trainingModeSync"), ("invalid", "trainingModeInvalid"), ("invalid-null", "trainingModeInvalid")):
        ab("open", f"{BASE}?view=detail&id={run_id}&tab=config")
        ab("wait", "--text", strings[key])
        assert strings[key] in snapshot()
        ab("reload")
        ab("wait", "--text", strings[key])
        assert strings[key] in snapshot()
        screenshot(f"{lang}-{run_id}.png")
        record(f"{lang} detail {run_id} + reload")

# Chinese new wizard; failed default metadata must not invent defaults or lose chosen mode.
click_button("中文")
# Hold failure through React StrictMode's double effect; release explicitly at Retry.
qa({"defaults_fail": 100})
ab("open", f"{BASE}?view=new")
ab("wait", "--text", "选择 Agent Runtime")
ab("click", ".v2-sel")
ab("press", "ArrowDown")
ab("press", "Enter")
next_step()
ab("click", ".v2-sel")
ab("press", "ArrowDown")
ab("press", "Enter")
next_step()
assert_js("document.querySelector('[role=radio][aria-checked=true]').textContent === '同步'")
assert_js("document.querySelector('[data-testid=tp-run-next]').disabled")
record("zh new default sync; metadata failure blocks next")
ab("focus", "[role=radio]")
ab("press", "Tab")
ab("press", "Space")
assert_js("document.querySelector('[role=radio][aria-checked=true]').textContent === '同池异步（实验性）'")
qa({"defaults_fail": 0})
click_button("重试")
ab("wait", "--fn", "!document.querySelector('[data-testid=tp-run-next]').disabled")
assert_js("document.querySelector('[role=radio][aria-checked=true]').textContent === '同池异步（实验性）'")
click_button("模拟小型预设")
record("zh keyboard async selection + metadata retry + preset preserve mode")
ab("set", "viewport", "390", "844")
ab("focus", "[role=radio][aria-checked=true]")
screenshot("zh-390-async-model.png")
width = ab("eval", "({viewport:innerWidth,scroll:document.documentElement.scrollWidth})")
record("390px mode control keyboard reachable", geometry=width)
next_step()
ab("wait", "--fn", "!!document.querySelector('[data-testid=tp-run-max-hours]')")
baseline = qa()
for value in ("", "0.00001"):
    number(value)
    assert_js("document.querySelector('[data-testid=tp-run-max-hours]').getAttribute('aria-invalid') === 'true'")
    assert_js("document.querySelector('[data-testid=tp-run-next]').disabled")
    assert qa()["previews"] == baseline["previews"] and qa()["creates"] == baseline["creates"]
    record(f"zh async max_hours={value!r} blocked with no request")
screenshot("zh-390-deadline-rejected.png")
number("0.01")
next_step()
ab("wait", "--fn", "!!document.querySelector('[data-testid=tp-run-start]')")
assert "同池异步（实验性）" in snapshot()
click_button("中文")
ab("click", '[data-testid="tp-run-start"]')
screenshot("zh-390-async-confirm.png")
before = qa()["creates"]
click_button("取消")
assert qa()["creates"] == before
record("zh 390px review/cost confirm mode; cancel zero create")
ab("click", '[data-testid="tp-run-start"]')
ab("click", '[data-testid="v2-confirm-ok"]')
ab("wait", "--fn", "location.search.includes('view=detail')")
state = qa()
assert state["creates"] == before + 1 and not state["violations"]
run_id = f"mock-{state['creates']}"
ab("open", f"{BASE}?view=detail&id={run_id}&tab=config")
ab("wait", "--text", "同池异步（实验性）")
ab("reload")
ab("wait", "--text", "同池异步（实验性）")
screenshot("zh-390-async-saved.png")
record("zh create exact preview payload; saved async survives reload", run_id=run_id)
print("BROWSER CHECKS COMPLETE", flush=True)

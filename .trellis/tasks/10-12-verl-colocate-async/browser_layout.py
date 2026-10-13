"""Bilingual narrow-layout regression using the owned mock/agent-browser services.
Run after browser_services.py WORK; no real control-plane server is used.
python3 browser_layout.py WORK
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
checks = []

def ab(*args):
    result = subprocess.run([sys.executable, str(TASK / "browser_cli.py"), str(WORK), *args],
                            text=True, capture_output=True, timeout=50)
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result.stdout.strip()


def qa(body=None):
    req = Request("http://127.0.0.1:18187/__qa", data=json.dumps(body).encode() if body else None,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=5) as response:
        return json.load(response)


def click(text):
    snap = ab("snapshot")
    match = re.search(r'- button "' + re.escape(text) + r'"[^\n]*ref=(e\d+)', snap)
    assert match, (text, snap)
    ab("click", "@" + match[1])


def js(expression):
    assert ab("eval", f"Boolean({expression})") == "true", expression


def next_step():
    ab("focus", "[data-testid=tp-run-next]")
    ab("press", "Enter")


def select_first():
    ab("click", ".v2-sel")
    ab("press", "ArrowDown")
    ab("press", "Enter")


def hours(value):
    ab("focus", "[data-testid=tp-run-max-hours]")
    ab("press", "Control+a")
    ab("press", "Backspace")
    if value:
        ab("keyboard", "type", value)
    ab("press", "Tab")


def geometry(name, selector):
    # No blanket overflow hiding: the document and each navigation item must fit.
    result = json.loads(ab("eval", """(() => {
      const root = document.documentElement;
      const nodes = [...document.querySelectorAll(%s)];
      return {viewport: innerWidth, client: root.clientWidth, scroll: root.scrollWidth,
        hidden: [root, document.body].some(e => ['hidden','clip'].includes(getComputedStyle(e).overflowX)),
        items: nodes.map(e => { const r=e.getBoundingClientRect();
          return {text:e.textContent, left:r.left, right:r.right, width:r.width}; })};
    })()""" % json.dumps(selector)))
    assert result["scroll"] <= result["client"], (name, result)
    assert not result["hidden"] and result["items"], (name, result)
    assert all(0 <= e["left"] < e["right"] <= result["client"] for e in result["items"]), (name, result)
    image = ab("screenshot", str(WORK / f"{name}.png"))
    checks.append({"check": name, "result": "PASS", "geometry": result, "screenshot": image})
    (WORK / "layout-checks.json").write_text(json.dumps(checks, indent=2, ensure_ascii=False))
    print(name, result["viewport"], result["client"], result["scroll"], "PASS", flush=True)


ab("open", f"{BASE}?view=new")
ab("wait", "--fn", "!!document.querySelector('.v2-lang')")
for lang, toggle in (("en", "EN"), ("zh-CN", "中文")):
    strings = json.loads((ROOT / f"frontend/src/locales/{lang}/common.json").read_text())
    labels = strings["runs"]
    click(toggle)
    ab("set", "viewport", "390", "844")
    ab("open", f"{BASE}?view=new")
    ab("wait", "--fn", "!!document.querySelector('.v2-sel')")
    geometry(f"{lang}-390-agent", ".v2-steps button")
    select_first()
    next_step()
    select_first()
    next_step()
    ab("wait", "--fn", "!!document.querySelector('[role=radio]') && !document.querySelector('[data-testid=tp-run-next]').disabled")
    js("document.querySelector('[role=radio][aria-checked=true]').textContent === " + json.dumps(labels["trainingModeSync"]))
    ab("focus", "[role=radio]")
    ab("press", "Tab")
    ab("press", "Space")
    js("document.querySelector('[role=radio][aria-checked=true]').textContent === " + json.dumps(labels["trainingModeAsync"]))
    click("Mock tiny preset" if lang == "en" else "模拟小型预设")
    # Back through the actual step button, then switch runtime/template and return.
    ab("focus", ".v2-steps button")
    ab("press", "Enter")
    ab("click", ".v2-sel")
    ab("press", "ArrowDown")
    ab("press", "Enter")
    next_step()
    next_step()
    ab("wait", "--fn", "!document.querySelector('[data-testid=tp-run-next]').disabled")
    js("document.querySelector('[role=radio][aria-checked=true]').textContent === " + json.dumps(labels["trainingModeAsync"]))
    ab("focus", "[role=radio][aria-checked=true]")
    geometry(f"{lang}-390-model", ".v2-steps button, [role=radio]")
    ab("set", "viewport", "1280", "900")
    geometry(f"{lang}-desktop-model", ".v2-steps button, [role=radio]")
    ab("set", "viewport", "390", "844")
    next_step()
    ab("wait", "--fn", "!!document.querySelector('[data-testid=tp-run-max-hours]')")
    baseline = qa()
    for value in ("", "0.00001"):
        hours(value)
        js("document.querySelector('[data-testid=tp-run-max-hours]').getAttribute('aria-invalid') === 'true'")
        js("document.querySelector('[data-testid=tp-run-next]').disabled")
        assert qa()["previews"] == baseline["previews"] and qa()["creates"] == baseline["creates"]
    geometry(f"{lang}-390-deadline", ".v2-steps button")
    hours("0.01")
    qa({"preview_fail": 1})
    next_step()
    ab("wait", "--text", "Service Unavailable")
    next_step()
    ab("wait", "--fn", "!!document.querySelector('[data-testid=tp-run-start]')")
    state = qa()
    assert state["last_preview"]["training_mode"] == "colocate_async"
    assert state["last_preview"]["agent_runtime_id"] == "rt-office"
    assert state["last_preview"]["params"] == {"train_batch_size": 8, "rollout_n": 4}
    assert labels["trainingModeAsync"] in ab("snapshot")
    geometry(f"{lang}-390-review", ".v2-steps button")
    ab("click", "[data-testid=tp-run-start]")
    geometry(f"{lang}-390-confirm", "[role=dialog]")
    before = qa()["creates"]
    click(strings["v2"]["common"]["cancel"])
    assert qa()["creates"] == before
    qa({"create_fail": 1})
    ab("click", "[data-testid=tp-run-start]")
    ab("click", "[data-testid=v2-confirm-ok]")
    ab("wait", "--text", "Service Unavailable")
    assert qa()["creates"] == before
    # Failed submit closes confirmation; require explicit cost confirmation again.
    ab("click", "[data-testid=tp-run-start]")
    ab("click", "[data-testid=v2-confirm-ok]")
    ab("wait", "--fn", "location.search.includes('view=detail')")
    state = qa()
    assert state["creates"] == before + 1 and not state["violations"]
    run_id = f"mock-{state['creates']}"
    ab("open", f"{BASE}?view=detail&id={run_id}&tab=config")
    ab("wait", "--text", labels["trainingModeAsync"])
    ab("reload")
    ab("wait", "--text", labels["trainingModeAsync"])
    geometry(f"{lang}-390-detail", ".v2-subtabs button")
    ab("focus", "[data-testid=v2-tab-config]")
    ab("press", "Enter")
    js("document.querySelector('[data-testid=v2-tab-config]').getAttribute('aria-selected') === 'true'")
    ab("set", "viewport", "1280", "900")
    geometry(f"{lang}-desktop-detail", ".v2-subtabs button")
assert not qa()["violations"]
assert not ab("errors")
print("BILINGUAL LAYOUT AND RETRY CHECKS COMPLETE", flush=True)

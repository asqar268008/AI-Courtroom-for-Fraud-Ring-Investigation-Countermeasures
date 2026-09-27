"""Check your real-LLM setup before the demo.

    python scripts/check_llm.py            # uses LLM_PROVIDER / LLM_API_KEY / LLM_MODEL from .env

1. prints the resolved provider, endpoint and models
2. sends one tiny JSON request (connectivity, key, model name, JSON mode)
3. runs the full golden case with the LLM writing the Prosecutor, Defense, Judge and SMS text and
   shows, for each agent, whether its text came from the LLM or fell back to the template.
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

os.environ.setdefault("FC_STAGE_DELAY", "0")
os.environ.setdefault("FC_TOKEN_DELAY", "0")
os.environ.setdefault("FC_SETTLE_WINDOW", "0")
os.environ["LLM_CACHE"] = "0"  # measure the real thing
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from database.graph_store import GraphStore  # noqa: E402
from core.llm import LLM, LLMError  # noqa: E402
from agents.moot_court.precedent_memory import Memory  # noqa: E402
from core.simulator import build_demo_scenario  # noqa: E402
from core.workflow import run_case  # noqa: E402


async def main() -> int:
    llm = LLM(settings)
    d = llm.describe()
    print(f"Provider : {d['provider']}\nEndpoint : {d['base_url']}\nModel    : {llm.model}\nJudge    : {llm.judge_model}")
    if not llm.enabled:
        print("\nLLM is OFF. Set LLM_PROVIDER (groq | gemini | openai | openrouter | ollama | anthropic) and "
              "LLM_API_KEY in .env (ollama needs no key).")
        return 1

    t = time.perf_counter()
    try:
        out = await llm.json("Reply as JSON.", 'Return {"ok": true, "word": "namaste"}', max_tokens=300)
        print(f"\n[1] Ping OK in {time.perf_counter() - t:.2f}s -> {out}")
    except LLMError as exc:
        print(f"\n[1] Ping FAILED: {exc}\n    Check the key, the model name (LLM_MODEL) and LLM_BASE_URL.")
        return 1

    sc = build_demo_scenario()
    store = GraphStore.from_records(sc["accounts"], sc["transactions"], sc["logins"])
    t = time.perf_counter()
    final, ctx = await run_case(store, Memory(Path(tempfile.mkdtemp()) / "m.json"), "A", sc["meta"]["t0"] + 70,
                                reason="LLM check", llm=llm)
    took = time.perf_counter() - t
    print(f"\n[2] Golden case in {took:.1f}s with the LLM")
    for e in ctx.recording:
        if e["type"] == "agent.message" and e["payload"]["agent"] in ("prosecutor", "defense", "judge"):
            p = e["payload"]
            print(f"\n  {p['agent'].upper():<10} [{p.get('source')}]\n  {p['text']}")
    for e in ctx.recording:
        if e["type"] == "customer.message":
            print(f"\n  SMS (tier {e['payload']['tier']}): {e['payload']['text']}")
    tiers = {v.node_id: v.tier for v in final["verdict"].node_verdicts}
    ok = tiers.get("C3") == 2 and tiers.get("A") == 4
    s = llm.describe()
    print(f"\n[3] Verdict unchanged by the LLM: {'yes' if ok else 'NO'} | calls {s['calls']}, failures {s['failures']}, "
          f"retries {s['retries']}, tokens {s['input_tokens']}+{s['output_tokens']}, avg latency {s['avg_latency_s']}s")
    if s["last_error"]:
        print(f"    last error: {s['last_error']}")
    await llm.aclose()
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

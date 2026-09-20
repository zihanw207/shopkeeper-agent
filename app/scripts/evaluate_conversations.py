"""多轮端到端小样本检查：每个场景新建会话，比较真实查询结果及持久化状态。"""

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import httpx

from app.evaluation.results import results_equal
from app.scripts.evaluate_agent import check_fixture

ROOT = Path(__file__).resolve().parents[2]


async def evaluate(args, scenarios):
    from app.clients.mysql_client_manager import dw_mysql_client_manager
    from app.conf.app_config import app_config
    from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository

    references = {}
    dw_mysql_client_manager.init()
    try:
        async with dw_mysql_client_manager.session_factory() as session:
            repository = DWMySQLRepository(session)
            for scenario in scenarios:
                for index, turn in enumerate(scenario["turns"]):
                    if "reference_sql" in turn:
                        checked = await repository.validate(turn["reference_sql"])
                        references[(scenario["id"], index)] = await repository.run(
                            checked
                        )
    finally:
        await dw_mysql_client_manager.close()

    records = []
    async with httpx.AsyncClient(base_url=args.base_url, timeout=150) as client:
        for scenario in scenarios:
            response = await client.post("/api/conversations")
            response.raise_for_status()
            cid = response.json()["id"]
            for index, turn in enumerate(scenario["turns"]):
                record = {
                    "scenario": scenario["id"],
                    "turn": index + 1,
                    "conversation_id": cid,
                    "query": turn["query"],
                    "passed": False,
                }
                started = perf_counter()
                try:
                    response = await client.post(
                        "/api/query",
                        json={"query": turn["query"], "conversation_id": cid},
                    )
                    response.raise_for_status()
                    events = [
                        json.loads(line[5:].strip())
                        for line in response.text.splitlines()
                        if line.startswith("data:")
                    ]
                    terminal = [
                        event
                        for event in events
                        if event["type"]
                        in {"result", "error", "clarification", "unsupported"}
                    ]
                    outcome = terminal[-1] if terminal else {"type": "missing"}
                    record.update(
                        outcome=outcome,
                        request_id=response.headers.get("x-request-id"),
                        context=next(
                            (event for event in events if event["type"] == "context"),
                            None,
                        ),
                    )
                    expected = references.get((scenario["id"], index))
                    passed = (
                        len(terminal) == 1 and outcome["type"] == turn["expected_type"]
                    )
                    if expected is not None:
                        record["expected"] = expected
                        passed = (
                            passed
                            and bool(expected)
                            and results_equal(
                                outcome.get("data"),
                                expected,
                                ordered=turn.get("ordered", False),
                            )
                        )
                    restored = await client.get(f"/api/conversations/{cid}")
                    restored.raise_for_status()
                    saved = restored.json()["turns"][-1]
                    record["persistence_matches"] = (
                        saved["outcome"] == outcome and saved["query"] == turn["query"]
                    )
                    record["passed"] = passed and record["persistence_matches"]
                except Exception as exc:
                    record["error"] = type(exc).__name__
                record["latency_ms"] = round((perf_counter() - started) * 1000, 2)
                records.append(record)
                print(
                    json.dumps(
                        {
                            key: record[key]
                            for key in ("scenario", "turn", "passed", "latency_ms")
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": app_config.llm.model_name,
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "prompt_sha256": hashlib.sha256(
            (ROOT / "prompts/resolve_conversation.prompt").read_bytes()
        ).hexdigest(),
        "passed": sum(record["passed"] for record in records),
        "total": len(records),
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    )
    print(f"{report['passed']}/{report['total']} 轮通过；报告：{args.output}")
    return report["passed"] == report["total"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--dataset", type=Path, default=ROOT / "evals/datasets/conversation_smoke.json"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "evals/reports/conversation-smoke.json"
    )
    parser.add_argument("--check-fixture", action="store_true")
    args = parser.parse_args()
    scenarios = json.loads(args.dataset.read_text())
    if args.check_fixture:
        check_fixture(
            [
                {
                    "id": f"{scenario['id']}:{index + 1}",
                    "reference_sql": turn["reference_sql"],
                }
                for scenario in scenarios
                for index, turn in enumerate(scenario["turns"])
                if "reference_sql" in turn
            ]
        )
    else:
        raise SystemExit(0 if asyncio.run(evaluate(args, scenarios)) else 1)


if __name__ == "__main__":
    main()

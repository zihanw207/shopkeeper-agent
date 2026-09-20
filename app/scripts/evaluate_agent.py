"""在线跑真实问数接口；--check-fixture 只验证种子集参考 SQL，不调用模型。"""

import argparse
import asyncio
import hashlib
import json
import math
import re
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import httpx

from app.agent.sql_policy import default_sql_policy, prepare_read_query
from app.evaluation.results import results_equal

ROOT = Path(__file__).resolve().parents[2]


def load_cases(path):
    cases = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = set()
    for case in cases:
        if (
            case["id"] in ids
            or not case["query"].strip()
            or not isinstance(case["ordered"], bool)
        ):
            raise ValueError("题目 ID 重复或题目格式错误")
        ids.add(case["id"])
        prepare_read_query(case["reference_sql"], default_sql_policy())
    if not cases:
        raise ValueError("评测集不能为空")
    return cases


def check_fixture(cases):
    """仅把仓库自带的 CREATE TABLE / INSERT 种子数据加载到内存 SQLite。"""
    seed = (ROOT / "docker/mysql/dw.sql").read_text(encoding="utf-8")
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        for match in re.finditer(
            r"(?:CREATE TABLE|INSERT INTO)\s+\w+\b.*?;", seed, re.I | re.S
        ):
            connection.executescript(match.group())
        for case in cases:
            sql = prepare_read_query(case["reference_sql"], default_sql_policy())
            rows = [dict(row) for row in connection.execute(sql)]
            print(
                json.dumps(
                    {"id": case["id"], "reference_rows": len(rows)}, ensure_ascii=False
                )
            )
    print(f"已检查 {len(cases)} 条参考查询；这不是模型准确率评测。")


async def collect_response(client, base_url, case, timeout):
    started = perf_counter()
    record = {"id": case["id"], "category": case["category"], "passed": False}
    payload = []
    async with asyncio.timeout(timeout):
        async with client.stream(
            "POST", f"{base_url.rstrip('/')}/api/query", json={"query": case["query"]}
        ) as response:
            record["request_id"] = response.headers.get("x-request-id")
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    payload.append(line[5:].lstrip())
                elif not line and payload:
                    event = json.loads("\n".join(payload))
                    payload.clear()
                    record.setdefault(
                        "first_event_ms", round((perf_counter() - started) * 1000, 2)
                    )
                    if event["type"] == "result":
                        record.update(
                            actual=event["data"],
                            candidate_sql=event.get("sql"),
                            sql_repair_count=event.get("sql_repair_count"),
                            analysis=event.get("analysis"),
                        )
                    elif event["type"] == "error":
                        record["error_code"] = event.get("code", "agent_error")
                        record["sql_repair_count"] = event.get("sql_repair_count")
            if payload:
                raise ValueError("SSE 事件没有结束分隔符")
    record["latency_ms"] = round((perf_counter() - started) * 1000, 2)
    return record


def percentile(values, percent):
    return (
        sorted(values)[max(0, math.ceil(len(values) * percent) - 1)] if values else None
    )


async def evaluate(args, cases):
    # 离线检查不导入应用配置，不要求 API Key，也不连接任何外部服务。
    from app.clients.mysql_client_manager import dw_mysql_client_manager
    from app.conf.app_config import app_config
    from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository

    references = {}
    dw_mysql_client_manager.init()
    try:
        async with dw_mysql_client_manager.session_factory() as session:
            repo = DWMySQLRepository(session)
            for case in cases:
                checked = await repo.validate(case["reference_sql"])
                references[case["id"]] = await repo.run(checked)
    finally:
        await dw_mysql_client_manager.close()

    if args.check_database:
        for case in cases:
            print(
                json.dumps(
                    {"id": case["id"], "reference_rows": len(references[case["id"]])},
                    ensure_ascii=False,
                )
            )
        print(f"已通过真实 MySQL 检查 {len(cases)} 条参考查询；未调用模型。")
        return

    records = []
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        for case in cases:
            started = perf_counter()
            try:
                record = await collect_response(
                    client, args.base_url, case, args.timeout
                )
                expected = references[case["id"]]
                record["passed"] = (
                    "error_code" not in record
                    and "actual" in record
                    and results_equal(
                        record["actual"], expected, ordered=case["ordered"]
                    )
                )
                record["expected"] = expected
                # 空结果碰巧相同不足以证明语义正确，单独报告，不计入主准确率。
                record["empty_reference"] = not expected
            except Exception as error:
                record = {
                    "id": case["id"],
                    "category": case["category"],
                    "passed": False,
                    "error_code": type(error).__name__,
                    "latency_ms": round((perf_counter() - started) * 1000, 2),
                    "empty_reference": not references[case["id"]],
                }
            records.append(record)
            print(
                json.dumps(
                    {
                        key: record.get(key)
                        for key in ("id", "passed", "latency_ms", "error_code")
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    primary = [record for record in records if not record["empty_reference"]]
    repaired = [
        record for record in primary if (record.get("sql_repair_count") or 0) > 0
    ]
    latencies = [record["latency_ms"] for record in records]
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    changed = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    source_hash = hashlib.sha256()
    for path in sorted((ROOT / "app").rglob("*.py")) + [
        ROOT / "main.py",
        ROOT / "pyproject.toml",
    ]:
        source_hash.update(str(path.relative_to(ROOT)).encode())
        source_hash.update(path.read_bytes())
    summary = {
        "tag": args.tag,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": app_config.llm.model_name,
        "commit": commit,
        "working_tree_dirty": bool(changed),
        "source_sha256": source_hash.hexdigest(),
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "prompt_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "prompts").glob("*.prompt"))
        },
        "reference_results_sha256": hashlib.sha256(
            json.dumps(references, sort_keys=True, default=str).encode()
        ).hexdigest(),
        "case_ids": [case["id"] for case in cases],
        "total": len(records),
        "primary_total": len(primary),
        "execution_accuracy_nonempty": sum(r["passed"] for r in primary) / len(primary)
        if primary
        else None,
        "first_pass_accuracy_nonempty": sum(
            r["passed"] and r.get("sql_repair_count") == 0 for r in primary
        )
        / len(primary)
        if primary
        else None,
        "repair_attempted_cases": len(repaired),
        "repair_result_accuracy": sum(r["passed"] for r in repaired) / len(repaired)
        if repaired
        else None,
        "errors": dict(Counter(r["error_code"] for r in records if "error_code" in r)),
        "empty_cases": [r["id"] for r in records if r["empty_reference"]],
        "p50_ms": percentile(latencies, 0.5),
        "p95_ms": percentile(latencies, 0.95),
        "token_usage": None,
        "cost": None,
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"报告已保存：{args.output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=ROOT / "evals/datasets/ecommerce_seed.jsonl"
    )
    verification = parser.add_mutually_exclusive_group()
    verification.add_argument("--check-fixture", action="store_true")
    verification.add_argument(
        "--check-database",
        action="store_true",
        help="只在当前 MySQL 上验证参考查询，不调用模型",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=150)
    parser.add_argument("--tag", default="sql-loop-v1")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "evals/reports/latest.json"
    )
    args = parser.parse_args()
    if (args.limit is not None and args.limit < 1) or args.timeout <= 0:
        parser.error("limit 和 timeout 必须为正数")
    cases = load_cases(args.dataset)
    cases = cases[: args.limit] if args.limit else cases
    if args.check_fixture:
        check_fixture(cases)
    else:
        asyncio.run(evaluate(args, cases))


if __name__ == "__main__":
    main()

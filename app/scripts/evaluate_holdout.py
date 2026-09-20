"""独立测试集：默认离线校验；真实模型评测必须显式指定 --run-online。"""

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from urllib.parse import urlparse

from app.evaluation.holdout import (
    TERMINALS,
    check_fixture,
    grade_turn,
    load_suite,
    parse_events,
    query_payload,
    select_cases,
    sha256,
)
from app.evaluation.results import results_equal
from app.evaluation.warehouse_fixture import ROOT
from app.scripts.extend_warehouse import pending_rows, read_rows, write_private_json


async def check_database(suite, goldens):
    """一致性快照内只读：核对所有行，再用真实 MySQL 执行参考查询和错误变体。"""
    from sqlalchemy import URL, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.agent.sql_policy import default_sql_policy, prepare_read_query
    from app.conf.app_config import app_config
    from app.evaluation.holdout import compare_table

    config = app_config.db_dw
    if config.host not in {"127.0.0.1", "localhost", "::1"} or config.database != "dw":
        raise ValueError("仅允许核对本机教学 dw")
    engine = create_async_engine(
        URL.create(
            "mysql+asyncmy",
            username=config.user,
            password=config.password,
            host=config.host,
            port=config.port,
            database=config.database,
            query={"charset": "utf8mb4"},
        ),
        hide_parameters=True,
        isolation_level="REPEATABLE READ",
        connect_args={"connect_timeout": 10},
    )
    references, mutants = 0, 0
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET SESSION MAX_EXECUTION_TIME = 10000"))
            await connection.execute(
                text("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            )
            if any(pending_rows(await read_rows(connection)).values()):
                raise ValueError("数据库缺少扩充数据")

            async def run(sql):
                checked = prepare_read_query(sql, default_sql_policy())
                async with asyncio.timeout(12):
                    await connection.execute(text(f"EXPLAIN {checked}"))
                    return [
                        dict(row)
                        for row in (await connection.execute(text(checked))).mappings()
                    ]

            for case in suite["cases"]:
                for turn in case["turns"]:
                    for reference in turn.get("references", []):
                        expected = goldens[f"{turn['id']}/{reference['id']}"]["rows"]
                        if not results_equal(await run(reference["sql"]), expected):
                            raise ValueError(
                                f"MySQL 参考答案不符：{turn['id']}/{reference['id']}"
                            )
                        references += 1
                    for mutant in turn.get("mutants", []):
                        expected = goldens[f"{turn['id']}/answer"]["rows"]
                        checked = compare_table(
                            await run(mutant["sql"]),
                            expected,
                            turn["grading"],
                            canonical_units=True,
                        )
                        if checked["status"] != "fail":
                            raise ValueError(
                                f"MySQL 错误变体未被区分：{turn['id']}/{mutant['id']}"
                            )
                        mutants += 1
            await connection.rollback()
    finally:
        await engine.dispose()
    return {
        "fixture_matches": True,
        "references_checked": references,
        "mutants_distinguished": mutants,
    }


def snapshot_sources():
    paths = [
        *ROOT.glob("app/**/*.py"),
        *ROOT.glob("prompts/*.prompt"),
        ROOT / "conf/meta_config.yaml",
    ]
    return {str(p.relative_to(ROOT)): sha256(p) for p in sorted(paths)}


async def collect(client, cases, goldens):
    """每个场景和 lane 新建会话；顺序交错检查隔离，不宣称测过并发执行。"""
    records = []
    for case in cases:
        conversations, broken_lanes = {}, set()
        for turn in case["turns"]:
            record = {
                "scenario": case["id"],
                "turn": turn["id"],
                "kind": case["kind"],
                "exposure": case["exposure"],
                "query": turn["query"],
                "human_review": {
                    "status": "pending",
                    "rubric": turn["grading"]["rubric"],
                    "critical_failures": turn["grading"]["critical_failures"],
                },
            }
            lane = turn.get("lane", "main")
            if case["execution"] == "mock_only" or lane in broken_lanes:
                record["automatic"] = {
                    "status": "not_run",
                    "reason": "仅隔离测试"
                    if case["execution"] == "mock_only"
                    else "同一会话前序请求异常",
                }
                records.append(record)
                continue
            started = perf_counter()
            try:
                if lane not in conversations:
                    response = await client.post("/api/conversations")
                    response.raise_for_status()
                    conversations[lane] = response.json()["id"]
                cid = conversations[lane]
                record.update(conversation_id=cid, lane=lane)
                response = await client.post(
                    "/api/query", json=query_payload(turn, cid)
                )
                response.raise_for_status()
                events = parse_events(response.text)
                record.update(
                    events=events,
                    request_id=response.headers.get("x-request-id"),
                    automatic=grade_turn(turn, events, goldens),
                )
                terminal = [e for e in events if e.get("type") in TERMINALS]
                restored = await client.get(f"/api/conversations/{cid}")
                restored.raise_for_status()
                saved = restored.json()["turns"][-1]
                record["persistence_matches"] = (
                    len(terminal) == 1
                    and saved["outcome"] == terminal[0]
                    and saved["query"] == turn["query"]
                )
                if not record["persistence_matches"]:
                    record["automatic"] = {
                        "status": "fail",
                        "reason": "持久化结果与返回结果不符",
                    }
            except Exception as exc:
                record["automatic"] = {
                    "status": "fail",
                    "reason": f"请求异常：{type(exc).__name__}",
                }
                broken_lanes.add(lane)
            record["latency_ms"] = round((perf_counter() - started) * 1000, 2)
            records.append(record)
            print(
                json.dumps(
                    {"turn": turn["id"], **record["automatic"]}, ensure_ascii=False
                ),
                flush=True,
            )
    return records


async def run_online(args, suite, goldens, frozen):
    import httpx

    from app.conf.app_config import app_config

    if urlparse(args.base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("本版本只支持本地后端，以便核对使用的数仓版本")
    if not args.case and not args.all:
        raise ValueError("在线运行须用 --case 选择完整场景，或显式使用 --all")
    selected = select_cases(suite, args.case)
    before = await check_database(suite, goldens)
    source_before = snapshot_sources()
    async with httpx.AsyncClient(base_url=args.base_url, timeout=150) as client:
        records = await collect(client, selected, goldens)
    postflight = {}
    try:
        postflight = await check_database(suite, goldens)
    except Exception as exc:
        postflight = {"fixture_matches": False, "error": type(exc).__name__}
    source_unchanged = source_before == snapshot_sources()
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "suite_manifest": frozen,
        "selected_scenarios": [c["id"] for c in selected],
        "configured_model": app_config.llm.model_name,
        "model_note": "取自本地配置；需确认当前后端已重启并使用相同配置。",
        "source_sha256": source_before,
        "source_unchanged": source_unchanged,
        "database_before": before,
        "database_after": postflight,
        "valid_environment": postflight.get("fixture_matches", False)
        and source_unchanged,
        "automatic_counts": dict(Counter(r["automatic"]["status"] for r in records)),
        "end_to_end_accuracy": None,
        "tokens": None,
        "cost": None,
        "note": "自动通过仅指数据与协议检查通过；人工结论复核未完成，不能当作完整 Agent 准确率。",
        "records": records,
    }
    write_private_json(args.output, report)
    print(f"报告：{args.output}；人工复核尚未完成。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-fixture", action="store_true")
    mode.add_argument("--check-database", action="store_true")
    mode.add_argument("--run-online", action="store_true")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--case", action="append", help="场景 ID，可重复；按场景完整运行"
    )
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    suite, goldens, frozen = load_suite()
    offline = check_fixture(suite, goldens)
    if args.check_database:
        print(
            json.dumps(asyncio.run(check_database(suite, goldens)), ensure_ascii=False)
        )
    elif args.run_online:
        args.output = args.output or ROOT / "evals/reports/holdout-v1" / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json"
        )
        if args.output.exists():
            raise ValueError("报告已存在，请换路径，避免覆盖原始实验记录")
        asyncio.run(run_online(args, suite, goldens, frozen))
    else:
        print(
            json.dumps(
                {**frozen["counts"], **offline, "model_calls": 0},
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

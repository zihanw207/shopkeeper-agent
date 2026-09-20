"""追加可复现的评测数据。默认仅离线预览；--apply 写本机 dw，绝不重跑初始化 SQL。"""

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from app.evaluation.results import results_equal
from app.evaluation.warehouse_fixture import (
    ROOT,
    TABLE_COLUMNS,
    VERSION,
    content_hash,
    expanded_connection,
    expected_rows,
    insert_sql,
    manifest,
    original_rows,
)

CASES = ROOT / "evals/datasets/warehouse_extension_v1_cases.json"


def cases():
    return json.loads(CASES.read_text())


def check_offline():
    connection = expanded_connection()
    try:
        for case in cases():
            actual = [dict(row) for row in connection.execute(case["reference_sql"])]
            if not results_equal(actual, case["expected"]):
                raise ValueError(f"离线参考结果不符：{case['id']}")
    finally:
        connection.close()
    return len(cases())


def pending_rows(current):
    """拒绝不相容数据，不覆盖已有行；重复执行只返回缺少的新增记录。"""
    expected = expected_rows()
    original = original_rows()
    pending = {}
    for table, columns in TABLE_COLUMNS.items():
        key = columns[0]
        expected_map = {row[key]: row for row in expected[table]}
        actual_map = {row[key]: row for row in current[table]}
        for value, row in actual_map.items():
            if value not in expected_map or row != expected_map[value]:
                raise ValueError(
                    f"{table} 存在扩展包以外或内容不一致的记录，已停止，未覆盖数据。"
                )
        if any(row[key] not in actual_map for row in original[table]):
            raise ValueError(f"{table} 缺少原始教学记录，请先检查数据库版本。")
        pending[table] = [row for row in expected[table] if row[key] not in actual_map]
    return pending


def write_private_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(data, output, ensure_ascii=False, indent=2, default=str)
        output.write("\n")


async def read_rows(connection):
    from sqlalchemy import text

    return {
        table: [
            dict(row)
            for row in (
                await connection.execute(
                    text(
                        f"SELECT {', '.join(columns)} FROM {table} ORDER BY {columns[0]}"
                    )
                )
            ).mappings()
        ]
        for table, columns in TABLE_COLUMNS.items()
    }


async def verify_live(connection):
    from sqlalchemy import text

    from app.agent.sql_policy import default_sql_policy, prepare_read_query

    current = await read_rows(connection)
    pending = pending_rows(current)
    if any(pending.values()):
        raise ValueError("扩展数据尚未完整写入。")
    checked = []
    for case in cases():
        sql = prepare_read_query(case["reference_sql"], default_sql_policy())
        rows = [dict(row) for row in (await connection.execute(text(sql))).mappings()]
        if not results_equal(rows, case["expected"]):
            raise ValueError(f"真实 MySQL 参考结果不符：{case['id']}")
        checked.append({"id": case["id"], "passed": True, "actual": rows})
    return checked


async def sync_quarter_values():
    """字段和实体不变；仅向现有 ES 值索引补齐季度枚举，不调用 Embedding。"""
    from elasticsearch import AsyncElasticsearch

    from app.conf.app_config import app_config

    config = app_config.es
    if config.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("只允许同步本机 Elasticsearch。")
    host = f"[{config.host}]" if ":" in config.host else config.host
    expected = [
        {
            "id": f"dim_date.quarter.Q{i}",
            "value": f"Q{i}",
            "column_id": "dim_date.quarter",
        }
        for i in range(1, 5)
    ]
    async with AsyncElasticsearch(
        f"http://{host}:{config.port}", request_timeout=15
    ) as client:
        if not await client.indices.exists(index="value_index"):
            raise ValueError("现有 value_index 不存在，请检查知识库服务。")
        response = await client.mget(
            index="value_index", ids=[item["id"] for item in expected]
        )
        operations = []
        for actual, wanted in zip(response["docs"], expected, strict=True):
            if actual.get("found"):
                if actual.get("_source") != wanted:
                    raise ValueError("季度索引内容与本项目定义不一致，未覆盖。")
            else:
                operations.extend(
                    [{"index": {"_index": "value_index", "_id": wanted["id"]}}, wanted]
                )
        if operations:
            response = await client.bulk(operations=operations, refresh="wait_for")
            if response.get("errors"):
                raise ValueError("季度索引部分写入失败；可重复同步。")
        actual = await client.mget(
            index="value_index", ids=[item["id"] for item in expected]
        )
        if [item.get("_source") for item in actual["docs"]] != expected:
            raise ValueError("季度索引校验失败。")
        return {
            "inserted": len(operations) // 2,
            "verified_values": ["Q1", "Q2", "Q3", "Q4"],
        }


async def database_action(action):
    from sqlalchemy import URL, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.conf.app_config import app_config

    config = app_config.db_dw
    if config.host not in {"localhost", "127.0.0.1", "::1"} or config.database != "dw":
        raise ValueError("脚本仅操作本机教学 dw 数据库。")
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
        connect_args={"connect_timeout": 10},
    )
    report = {"manifest": manifest(), "action": action}
    folder = (
        ROOT
        / "evals/reports"
        / VERSION
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    try:
        async with engine.connect() as connection:
            if action == "check":
                current = await read_rows(connection)
                pending = pending_rows(current)
                return {
                    **report,
                    "current_counts": {k: len(v) for k, v in current.items()},
                    "pending_counts": {k: len(v) for k, v in pending.items()},
                }
            if action == "verify":
                return {**report, "checks": await verify_live(connection)}

            acquired = (
                await connection.execute(
                    text("SELECT GET_LOCK('shopkeeper:warehouse-extension-v1', 5)")
                )
            ).scalar()
            await connection.commit()
            if acquired != 1:
                raise ValueError("已有扩展任务运行，请稍后重试。")
            try:
                async with connection.begin():
                    engines = (
                        await connection.execute(
                            text(
                                "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()"
                            )
                        )
                    ).all()
                    if any(
                        dict(engines).get(table) != "InnoDB" for table in TABLE_COLUMNS
                    ):
                        raise ValueError(
                            "目标表必须全部使用 InnoDB，才能保证事务回滚。"
                        )
                    current = await read_rows(connection)
                    pending = pending_rows(current)
                    report["inserted_counts"] = {k: len(v) for k, v in pending.items()}
                    report["before_sha256"] = content_hash(current)
                    if any(pending.values()):
                        write_private_json(
                            folder / "before.json",
                            {"version": VERSION, "tables": current},
                        )
                        report["backup"] = str(folder / "before.json")
                    # 插入缺少的主键；禁止 REPLACE / UPDATE / INSERT IGNORE 掩盖冲突。
                    for table in ("dim_date", "fact_order"):
                        rows = pending[table]
                        for index in range(0, len(rows), 500):
                            await connection.execute(
                                text(insert_sql(table, named=True)),
                                rows[index : index + 500],
                            )
                    # 在提交前检查全部行、原样例和每个场景，失败会回滚整笔事务。
                    report["checks"] = await verify_live(connection)
                report["warehouse_committed"] = True
            finally:
                await connection.execute(
                    text("SELECT RELEASE_LOCK('shopkeeper:warehouse-extension-v1')")
                )
                await connection.commit()
    finally:
        await engine.dispose()

    try:
        report["quarter_index"] = await sync_quarter_values()
    except Exception as error:
        report["quarter_index"] = {
            "status": "failed",
            "error_type": type(error).__name__,
        }
        write_private_json(folder / "report.json", report)
        raise ValueError(
            f"MySQL 已提交，季度索引同步失败；可运行 --sync-values 重试。报告：{folder / 'report.json'}"
        ) from None
    write_private_json(folder / "report.json", report)
    report["report_path"] = str(folder / "report.json")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    for name in ("preview", "check", "apply", "verify", "sync-values"):
        mode.add_argument(f"--{name}", action="store_true")
    parser.add_argument("--output", type=Path, help="保存预览清单（只适用于离线预览）")
    args = parser.parse_args()
    try:
        count = check_offline()
        if args.sync_values:
            result = asyncio.run(sync_quarter_values())
        elif args.apply or args.check or args.verify:
            result = asyncio.run(
                database_action(
                    "apply" if args.apply else "check" if args.check else "verify"
                )
            )
        else:
            result = {**manifest(), "offline_reference_checks": count}
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n"
                )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    except Exception as error:
        # 不把驱动中的连接串、参数或凭证打印到日志。
        parser.exit(1, f"操作失败：{type(error).__name__}；请检查本机服务连接。\n")


if __name__ == "__main__":
    main()

"""可复现的教学数仓增量数据；保留原始 2025 Q1 样例，不调用任何模型。"""

import calendar
import hashlib
import json
import random
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION = "warehouse-extension-v1"
ORDER_PREFIX = "EV1_"
RANDOM_SEED = 20260919
TABLE_COLUMNS = {
    "dim_region": ("region_id", "province", "region_name", "country"),
    "dim_customer": ("customer_id", "customer_name", "gender", "member_level"),
    "dim_product": ("product_id", "product_name", "category", "brand"),
    "dim_date": ("date_id", "year", "quarter", "month", "day"),
    "fact_order": (
        "order_id",
        "customer_id",
        "product_id",
        "date_id",
        "region_id",
        "order_quantity",
        "order_amount",
    ),
}
# 按地区和月份划出完整场景，背景订单不得混入。
RESERVED = {
    (2025, month, region)
    for month, regions in {
        4: ("R001", "R002", "R005"),
        5: ("R001", "R002", "R005"),
        6: ("R004",),
        7: ("R004",),
        8: ("R003", "R006"),
        9: ("R003", "R006"),
        10: ("R006",),
    }.items()
    for region in regions
}
PRICE_CENTS = (
    899900,
    949900,
    699900,
    549900,
    320000,
    89900,
    129900,
    19900,
    59900,
    2500,
    500,
    500,
    350,
    139900,
    89900,
)


def seed_connection():
    """只读取仓库内 CREATE/INSERT 到内存，不执行原初始化文件的 DROP/GRANT。"""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    seed = (ROOT / "docker/mysql/dw.sql").read_text(encoding="utf-8")
    for match in re.finditer(
        r"(?:CREATE TABLE|INSERT INTO)\s+\w+\b.*?;", seed, re.I | re.S
    ):
        connection.executescript(match.group())
    return connection


def original_rows():
    connection = seed_connection()
    try:
        return {
            table: [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} ORDER BY {columns[0]}"
                )
            ]
            for table, columns in TABLE_COLUMNS.items()
        }
    finally:
        connection.close()


def extension_rows():
    """24 个月日历 + 9725 笔新增订单；主键稳定，金额为整数或 0.5 的倍数。"""
    dates, orders = [], []
    day = date(2024, 1, 1)
    while day <= date(2025, 12, 31):
        if not (day.year == 2025 and day.month <= 3):
            dates.append(
                {
                    "date_id": int(day.strftime("%Y%m%d")),
                    "year": day.year,
                    "quarter": f"Q{(day.month - 1) // 3 + 1}",
                    "month": day.month,
                    "day": day.day,
                }
            )
        day += timedelta(days=1)

    def add(year, month, day, region, product, quantity, amount):
        orders.append(
            {
                "order_id": f"{ORDER_PREFIX}{year}{month:02d}{day:02d}_{len(orders) + 1:06d}",
                "customer_id": f"C{len(orders) % 20 + 1:03d}",
                "product_id": product,
                "date_id": year * 10000 + month * 100 + day,
                "region_id": region,
                "order_quantity": quantity,
                "order_amount": amount,
            }
        )

    rng = random.Random(RANDOM_SEED)
    # 每个非场景地区/月生成 80 单；月首月末保证有记录，包含闰日和跨年边界。
    for year in (2024, 2025):
        for month in range(1, 13):
            if year == 2025 and month <= 3:
                continue
            days = calendar.monthrange(year, month)[1]
            for region_number in range(1, 7):
                region = f"R{region_number:03d}"
                if (year, month, region) in RESERVED:
                    continue
                for i in range(80):
                    product_number = rng.randrange(1, 16)
                    quantity = (
                        rng.choice((1, 1, 2))
                        if product_number < 10
                        else rng.choice((1, 2, 3, 5, 8, 12))
                    )
                    factor = rng.choice((8500, 9500, 10000, 10500)) + (
                        1000 if month in (6, 11, 12) else 0
                    )
                    cents = PRICE_CENTS[product_number - 1] * quantity * factor // 10000
                    amount = (cents // 50) / 2
                    selected_day = (
                        1 if i == 0 else days if i == 1 else rng.randint(1, days)
                    )
                    add(
                        year,
                        month,
                        selected_day,
                        region,
                        f"P{product_number:03d}",
                        quantity,
                        amount,
                    )

    def batch(month, regions, product, count, amount, quantity=4):
        days = calendar.monthrange(2025, month)[1]
        for i in range(count):
            add(
                2025,
                month,
                i % days + 1,
                regions[i % len(regions)],
                product,
                quantity,
                amount,
            )

    # 华东：平均订单金额不变，订单数 100 -> 60，销售额 10000 -> 6000。
    batch(4, ("R002", "R005"), "P010", 100, 100)
    batch(5, ("R002", "R005"), "P010", 60, 100)
    # 华南：订单数均为 100，平均订单金额 100 -> 80。
    batch(4, ("R001",), "P010", 100, 100)
    batch(5, ("R001",), "P010", 100, 80)
    # 华北：数码减少 10000，食品增加 5000，整体净下降 5000。
    batch(6, ("R004",), "P001", 20, 1000, 1)
    batch(6, ("R004",), "P010", 100, 100)
    batch(7, ("R004",), "P001", 10, 1000, 1)
    batch(7, ("R004",), "P010", 150, 100)
    # 西南：零金额订单与无订单不同；零基期普通增长率没有定义。
    batch(8, ("R003",), "P010", 10, 0)
    batch(9, ("R003",), "P010", 10, 100)
    # 华中 8 月没有订单；9 月有 12 单，其中 2 单金额未知，NULL 不应转成 0。
    batch(9, ("R006",), "P010", 10, 100)
    batch(9, ("R006",), "P010", 2, None)
    # 华中 10 月：两个商品并列第一，第三个商品低于它们。
    batch(10, ("R006",), "P001", 5, 1000, 1)
    batch(10, ("R006",), "P002", 5, 1000, 1)
    batch(10, ("R006",), "P003", 3, 1000, 1)
    return {"dim_date": dates, "fact_order": orders}


def expected_rows():
    original = original_rows()
    for table, rows in extension_rows().items():
        original[table].extend(rows)
        original[table].sort(key=lambda row: row[TABLE_COLUMNS[table][0]])
    return original


def insert_sql(table, *, named=False):
    columns = TABLE_COLUMNS[table]
    values = ", ".join(f":{column}" if named else "?" for column in columns)
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({values})"


def expanded_connection():
    connection = seed_connection()
    for table, rows in extension_rows().items():
        connection.executemany(
            insert_sql(table),
            [tuple(row[column] for column in TABLE_COLUMNS[table]) for row in rows],
        )
    connection.commit()
    return connection


def content_hash(rows):
    return hashlib.sha256(
        json.dumps(
            rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def manifest():
    extra = extension_rows()
    return {
        "version": VERSION,
        "synthetic": True,
        "random_seed": RANDOM_SEED,
        "date_range": ["2024-01-01", "2025-12-31"],
        "preserved_sample_period": ["2025-01-01", "2025-03-31"],
        "order_id_prefix": ORDER_PREFIX,
        "insert_counts": {table: len(rows) for table, rows in extra.items()},
        "final_counts": {table: len(rows) for table, rows in expected_rows().items()},
        "extension_sha256": content_hash(extra),
        "seed_file_sha256": hashlib.sha256(
            (ROOT / "docker/mysql/dw.sql").read_bytes()
        ).hexdigest(),
        "notes": [
            "全部为教学合成数据，不代表真实经营原因。",
            "2025 Q1 原有样例保持不变；与新增年份比较可验证 SQL，但不能解释为真实经营趋势。",
            "华中 2025-08 无订单；华中 2025-09 两笔金额缺失；西南 2025-08 有订单但金额为零。",
            "日历完整不等于业务数据接入完整，没有库存、退款、支付状态或流量数据。",
        ],
    }

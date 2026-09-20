"""冻结测试集及离线判分；参考答案不会发送到 Agent。"""

import hashlib
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from app.evaluation.results import cell_equal, results_equal
from app.evaluation.warehouse_fixture import ROOT, expanded_connection, manifest

SUITE = ROOT / "evals/holdout/v1"
TERMINALS = {"result", "error", "clarification", "unsupported"}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def suite_counts(cases):
    turns = [turn for case in cases for turn in case["turns"]]
    return {
        "scenarios": len(cases),
        "turns": len(turns),
        "reference_queries": sum(len(t.get("references", [])) for t in turns),
        "mutants": sum(len(t.get("mutants", [])) for t in turns),
        "by_kind": dict(Counter(c["kind"] for c in cases)),
        "by_exposure": dict(Counter(c["exposure"] for c in cases)),
        "mock_only_scenarios": sum(c["execution"] == "mock_only" for c in cases),
    }


def load_suite(folder=SUITE):
    frozen = json.loads((folder / "manifest.json").read_text())
    for name in ("cases.json", "goldens.json"):
        if sha256(folder / name) != frozen["sha256"][name]:
            raise ValueError(
                f"冻结测试集已变化：{name}；请建立新版本，不能自动更新答案。"
            )
    suite = json.loads((folder / "cases.json").read_text())
    goldens = json.loads((folder / "goldens.json").read_text())
    fixture = manifest()
    for key in ("version", "extension_sha256", "seed_file_sha256", "final_counts"):
        if fixture[key] != frozen["fixture"][key]:
            raise ValueError(f"数据版本不匹配：{key}")
    if suite_counts(suite["cases"]) != frozen["counts"]:
        raise ValueError("测试集数量与冻结清单不一致")
    ids = [t["id"] for c in suite["cases"] for t in c["turns"]]
    if len(set(ids)) != len(ids):
        raise ValueError("题目 ID 重复")
    references = {
        f"{t['id']}/{r['id']}"
        for c in suite["cases"]
        for t in c["turns"]
        for r in t.get("references", [])
    }
    if references != set(goldens):
        raise ValueError("参考答案 ID 不完整")
    return suite, goldens, frozen


def check_fixture(suite, goldens):
    """只校验冻结答案，不改写；错误 SQL 必须可执行且与正确结果不同。"""
    from app.agent.sql_policy import default_sql_policy, prepare_read_query

    connection = expanded_connection()
    checked, distinguished = 0, 0
    try:
        for case in suite["cases"]:
            for turn in case["turns"]:
                for reference in turn.get("references", []):
                    prepare_read_query(reference["sql"], default_sql_policy())
                    cursor = connection.execute(reference["sql"])
                    rows = [dict(row) for row in cursor]
                    golden = goldens[f"{turn['id']}/{reference['id']}"]
                    if (
                        [c[0] for c in cursor.description] != golden["columns"]
                        or len(rows) > 200
                        or not results_equal(rows, golden["rows"])
                    ):
                        raise ValueError(f"离线参考答案不符：{turn['id']}")
                    checked += 1
                for mutant in turn.get("mutants", []):
                    prepare_read_query(mutant["sql"], default_sql_policy())
                    rows = [dict(row) for row in connection.execute(mutant["sql"])]
                    expected = goldens[f"{turn['id']}/answer"]["rows"]
                    result = compare_table(
                        rows, expected, turn["grading"], canonical_units=True
                    )
                    if result["status"] != "fail":
                        raise ValueError(
                            f"错误写法未被区分：{turn['id']}/{mutant['id']}"
                        )
                    distinguished += 1
    finally:
        connection.close()
    return {"references_checked": checked, "mutants_distinguished": distinguished}


def verdict(status, reason):
    return {"status": status, "reason": reason}


def compare_table(actual, expected, grading, *, canonical_units=False):
    """按语义列别名匹配；未知别名/转置表交给复核，不任意排列数字凑答案。"""
    if not isinstance(actual, list) or any(not isinstance(row, dict) for row in actual):
        return verdict("fail", "返回数据不是表格")
    if not actual or not expected:
        return verdict("pass" if actual == expected else "fail", "空结果比较")
    keys = set(actual[0])
    if any(set(row) != keys for row in actual):
        return verdict("needs_review", "各行列名不一致")
    mapping = {}
    for column in grading["columns"]:
        matches = keys.intersection(column["aliases"])
        if len(matches) != 1:
            return verdict("needs_review", f"列名或结果布局需要映射：{column['key']}")
        mapping[column["key"]] = matches.pop()
    if len(set(mapping.values())) != len(mapping):
        return verdict("needs_review", "同一列对应多个指标")
    if len(actual) != len(expected):
        return verdict("fail", "结果行数不符")

    def row_equal(left, right):
        return all(
            cell_equal(left[mapping[c["key"]]], right[c["key"]], Decimal(c["abs_tol"]))
            for c in grading["columns"]
        )

    if grading["ordered"]:
        equal = all(row_equal(a, e) for a, e in zip(actual, expected))
    else:
        # 保留重复行；使用匹配而非 set 或逐行贪心，兼容数值容差。
        edges = [[j for j, e in enumerate(expected) if row_equal(a, e)] for a in actual]
        matched = {}

        def assign(i, visited):
            for j in edges[i]:
                if j in visited:
                    continue
                visited.add(j)
                if j not in matched or assign(matched[j], visited):
                    matched[j] = i
                    return True
            return False

        equal = all(assign(i, set()) for i in range(len(actual)))
    if equal:
        return verdict("pass", "要求的数据列与冻结答案一致；文字结论另行复核")
    # 百分数与比例、小数精度、元与万元等展示转换不应被草率认作业务错误。
    if not canonical_units and any(
        c["key"].endswith(("_pct", "_pp")) for c in grading["columns"]
    ):
        return verdict(
            "needs_review", "数值不匹配；先核实百分数单位与精度，再判业务对错"
        )
    return verdict("fail", "要求的数据、数值或指定排序不符")


def parse_events(body):
    events = []
    # 支持 SSE 同一事件有多个 data 行；空行才结束一个事件。
    for block in body.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")
        )
        if data:
            event = json.loads(data)
            if not isinstance(event, dict) or "type" not in event:
                raise ValueError("SSE 事件格式不符")
            events.append(event)
    return events


def grade_turn(turn, events, goldens):
    terminal = [e for e in events if e.get("type") in TERMINALS]
    if len(terminal) != 1:
        return verdict("fail", "应收到且只收到一个终态事件")
    outcome = terminal[0]
    if outcome["type"] not in turn["expected_types"]:
        return verdict("fail", f"终态类型不符：{outcome['type']}")
    grading = turn["grading"]
    if grading["must_skip_sql"] and any(
        e.get("step") in {"生成SQL", "校验SQL", "执行SQL"} or e.get("sql")
        for e in events
    ):
        return verdict("fail", "应该先澄清或拒绝，却进入 SQL 路径")
    if grading["mode"] == "table":
        return compare_table(
            outcome.get("data"), goldens[f"{turn['id']}/answer"]["rows"], grading
        )
    if grading["mode"] == "analysis":
        analysis = outcome.get("analysis")
        if not isinstance(analysis, dict) or not analysis.get("summary"):
            return verdict("fail", "只返回数据，没有分析报告")
        if (
            grading.get("analysis_status")
            and analysis.get("status") != grading["analysis_status"]
        ):
            return verdict("fail", "数据不完整时未标记为部分结论")
    return verdict(
        "needs_review", "终态符合要求；澄清内容、结论与证据须按 rubric 人工复核"
    )


def query_payload(turn, conversation_id):
    """严格只传用户问题与会话 ID，杜绝 rubric/参考 SQL/标准答案泄露。"""
    return {"query": turn["query"], "conversation_id": conversation_id}


def select_cases(suite, case_ids):
    wanted = set(case_ids or [c["id"] for c in suite["cases"]])
    if wanted - {c["id"] for c in suite["cases"]}:
        raise ValueError("存在未知场景 ID；只能选择整个场景，不能抽取半段对话")
    return [c for c in suite["cases"] if c["id"] in wanted]

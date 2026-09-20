"""结果比较：显式列顺序、可选行顺序、数值误差、保留重复行。"""

from decimal import Decimal, InvalidOperation


def cell_equal(actual, expected, abs_tol=Decimal("0.01")):
    if actual is None or expected is None:
        return actual is expected
    # 参考值是数字时，接受 SSE 中 Decimal 序列化后的数字字符串。
    if isinstance(expected, (int, float, Decimal)) and not isinstance(expected, bool):
        if isinstance(actual, bool):
            return False
        try:
            left, right = Decimal(str(actual)), Decimal(str(expected))
            return (
                left.is_finite() and right.is_finite() and abs(left - right) <= abs_tol
            )
        except InvalidOperation, ValueError:
            return False
    return actual == expected


def results_equal(actual, expected, *, ordered=False):
    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    if any(not isinstance(row, dict) for row in actual):
        return False
    actual_rows = [list(row.values()) for row in actual]
    expected_rows = [list(row.values()) for row in expected]

    def row_equal(left, right):
        return len(left) == len(right) and all(
            cell_equal(a, e) for a, e in zip(left, right)
        )

    if ordered:
        return all(row_equal(a, e) for a, e in zip(actual_rows, expected_rows))
    # 二分图匹配处理重复行和浮点容差，避免 set 去重或贪心匹配误判。
    edges = [
        [j for j, e in enumerate(expected_rows) if row_equal(a, e)] for a in actual_rows
    ]
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

    return all(assign(i, set()) for i in range(len(actual_rows)))

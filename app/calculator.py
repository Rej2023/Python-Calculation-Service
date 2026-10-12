import networkx as nx
from asteval import Interpreter
import re


def build_dependency_graph(rows: list[dict], edges: list[tuple[str, str]]) -> list[dict]:
    """
    Topological sort using networkx.
    `edges` are (depends_on_code, metric_code) pairs already validated and
    persisted by MetricRepository (C#) at formula-save-time — no more
    regex-deriving them here on every calculation... in principle. In
    practice, a metric whose MetricDependency rows were never populated
    (seed/import data that bypassed MetricRepository's normal
    SyncMetricDependenciesAsync path, for instance) has NO persisted edges
    at all, so topological_sort has nothing constraining its position and
    can place it before its own dependencies. That's harmless for a
    metric coded with letters (resolve_formula just can't find it yet,
    raises, and the cell is defaulted to 0 with a logged issue) but
    silently WRONG for one coded with pure digits (e.g. "201"): evaluated
    before it's computed, its code is indistinguishable from the literal
    number 201, so the formula referencing it quietly uses that constant
    instead — no error, just a wrong answer. Deriving edges additionally
    from each CALC row's own formula text (same word-boundary substring
    match C#'s own dependency extraction uses, immune to the same
    ambiguity) makes the graph self-healing regardless of whether the DB
    table is complete — it only ever adds edges that are already true per
    the formula, so merging with the persisted ones is always safe.
    """
    graph = nx.DiGraph()

    # Add all nodes first
    for row in rows:
        graph.add_node(row["MetricCode"])

    # Only wire edges where both ends are metrics actually present in this
    # row set (e.g. filtered by product/project) — an edge pointing outside
    # it isn't a cycle, just not relevant here.
    codes = {row["MetricCode"] for row in rows}
    for depends_on_code, metric_code in edges:
        if depends_on_code in codes and metric_code in codes:
            graph.add_edge(depends_on_code, metric_code)

    code_patterns = {
        code: re.compile(rf'(?<![A-Z0-9_]){re.escape(code.upper())}(?![A-Z0-9_])')
        for code in codes
    }
    for row in rows:
        if row["Type"] != 2 or not row.get("Formula"):
            continue
        metric_code = row["MetricCode"]
        formula_upper = row["Formula"].upper()
        for other_code, pattern in code_patterns.items():
            if other_code != metric_code and pattern.search(formula_upper):
                graph.add_edge(other_code, metric_code)

    # Topological sort
    try:
        sorted_codes = list(nx.topological_sort(graph))
    except nx.NetworkXUnfeasible:
        raise Exception("Circular dependency detected in formulas")

    # Reorder rows as per sorted metric codes
    code_to_row = {row["MetricCode"]: row for row in rows}
    sorted_rows = []
    for code in sorted_codes:
        if code in code_to_row:
            sorted_rows.append(code_to_row[code])

    return sorted_rows


def resolve_formula(
    formula: str, metric_values: dict[str, float], known_codes: set[str] = frozenset()
) -> tuple[float, str | None]:
    """
    Replace MetricCode tokens with their values and evaluate safely.

    Example:
        formula = "REVENUE - COST_OF_SALES"
        metric_values = { "REVENUE": 5000000, "COST_OF_SALES": 3200000 }
        result = (1800000, None)

    Returns (value, warning). `warning` is set instead of raising when the
    formula itself is valid but the arithmetic can't be done as written
    (division/modulo by zero, overflow) — callers should record it and use
    the fallback value rather than treat it as a broken formula.

    `known_codes` is every metric Code (uppercase) that's actually part of
    this calculation. A metric coded with letters (e.g. "REVENUE") that
    hasn't been computed yet just fails the is_number check below and
    falls through to "Unknown token" — loud and obvious. A metric coded
    with pure digits (e.g. "201") is indistinguishable from the literal
    number 201 once it isn't yet in metric_values, so without this check
    it would silently fall through to "pure numeric constant" instead —
    wrong, but no error raised. Checking known_codes BEFORE is_number
    closes that gap: a recognized metric code that isn't computed yet is
    always a dependency-order problem, never a legitimate literal.
    """
    # Tokenize
    tokens = re.findall(r'[A-Za-z_][A-Za-z0-9_]*|[\d.]+|[+\-*/()%]', formula)

    resolved_tokens = []
    for token in tokens:
        token_key = token.upper()
        if token_key in metric_values:
            # MetricCode → replace with numeric value. Compared uppercase since
            # Metrics.Code is always stored uppercase (MetricRepository forces
            # ToUpperInvariant on save), but formula text may reference it in
            # any case (e.g. driver codes typed as "d1").
            value = metric_values[token_key]
            if value is None:
                raise ValueError(f"Metric '{token}' has no value (None). Cannot calculate.")
            resolved_tokens.append(str(value))
        elif token_key in known_codes:
            raise ValueError(
                f"Metric '{token}' referenced in '{formula}' before it was calculated "
                f"— dependency order problem, not a missing value"
            )
        elif is_number(token):
            # Pure numeric constant → keep as is
            resolved_tokens.append(token)
        elif token in ['+', '-', '*', '/', '(', ')', '%']:
            # Operator → keep as is
            resolved_tokens.append(token)
        else:
            raise ValueError(f"Unknown token '{token}' in formula: {formula}")

    resolved_expression = ' '.join(resolved_tokens)

    # Safe evaluation using asteval
    aeval = Interpreter()
    result = aeval(resolved_expression)

    if aeval.error:
        # Every token above was already validated as a known metric or a
        # numeric/operator literal, so resolved_expression is pure arithmetic
        # on floats. The only way aeval can fail from here is an arithmetic
        # problem (division/modulo by zero, overflow) — that's a data
        # condition (e.g. a zero denominator), not a broken formula, so
        # default to 0 instead of aborting the calculation.
        return 0.0, f"Arithmetic error in '{formula}' ({aeval.error_msg}) — defaulted to 0"

    return float(result), None


def is_number(token: str) -> bool:
    """Check if token is a pure numeric value."""
    try:
        float(token)
        return True
    except ValueError:
        return False


def calculate_all(rows: list[dict], dependency_edges: list[tuple[str, str]]) -> tuple[list, list, list]:
    """
    Calculate per TimeId. `rows` is already scoped to one TimePeriod by the
    caller's DB query, so TimeId here just means "which period slot" — it can
    range 1-12 (CALENDAR/FINANCIAL/PERIODS_12) or 1-13 (PERIODS_13); nothing
    in this function assumes a fixed count.

    The dependency structure (which metric depends on which) is the same for
    every period — only the values differ — so the topological order is
    built ONCE from `dependency_edges` and reused for every period, instead
    of rebuilding it per period.

    A cell that fails to calculate (missing driver value, etc.) no longer
    aborts the whole request: it's defaulted to 0, recorded in `errors`, and
    the rest of the calculation continues.

    Returns (updates, total_updates, issues) where issues is a list of
    {time_id, metric_id, metric_code, message, level} — level is "warning"
    for an auto-handled arithmetic default (e.g. divide-by-zero), "error"
    for anything else that had to be defaulted.
    """
    from collections import defaultdict

    # Group rows by TimeId
    periods = defaultdict(list)
    for row in rows:
        periods[row["TimeId"]].append(row)

    # Build the topological order once, from whichever period has rows.
    any_period_rows = next(iter(periods.values()))
    sorted_codes = [row["MetricCode"] for row in build_dependency_graph(any_period_rows, dependency_edges)]
    known_codes = {code.upper() for code in sorted_codes}

    all_updates = []
    metric_period_results = defaultdict(list)
    issues = []

    for time_id, period_rows in sorted(periods.items()):
        code_to_row = {row["MetricCode"]: row for row in period_rows}
        sorted_rows = [code_to_row[code] for code in sorted_codes if code in code_to_row]

        # Build values map for this period
        metric_values = {}
        for row in sorted_rows:
            if row["Type"] == 1:
                metric_values[row["MetricCode"]] = row["Amount"]
                metric_period_results[row["MetricId"]].append({
                    "time_id": time_id,
                    "amount": row["Amount"] if row["Amount"] else 0,
                    "time_behavior": row["TimeBehavior"]
                })

        # Calculate formula rows for this period
        for row in sorted_rows:
            if row["Type"] == 2 and row["Formula"]:
                try:
                    result, warning = resolve_formula(row["Formula"], metric_values, known_codes)
                    if warning:
                        issues.append({
                            "time_id": time_id,
                            "metric_id": row["MetricId"],
                            "metric_code": row["MetricCode"],
                            "message": warning,
                            "level": "warning"
                        })
                except Exception as e:
                    # Don't abort the rest of the calculation — default this
                    # cell to 0, flag it, and keep going so everything else
                    # (this period and the others) still calculates.
                    result = 0.0
                    issues.append({
                        "time_id": time_id,
                        "metric_id": row["MetricId"],
                        "metric_code": row["MetricCode"],
                        "message": f"[TimeId={time_id}] Error in '{row['MetricCode']}': {str(e)}",
                        "level": "error"
                    })

                metric_values[row["MetricCode"]] = result
                all_updates.append({
                    "id": row["Id"],
                    "amount": result,
                    "metric_id": row["MetricId"],
                    "time_behavior": row["TimeBehavior"]
                })
                metric_period_results[row["MetricId"]].append({
                    "time_id": time_id,
                    "amount": result,
                    "time_behavior": row["TimeBehavior"]
                })

    # Calculate TotalAmount per metric
    total_updates = []
    for metric_id, period_list in metric_period_results.items():
        time_behavior = period_list[0]["time_behavior"]
        amounts = [p["amount"] for p in period_list]
        non_zero = [a for a in amounts if a != 0]

        if time_behavior == 1:  # Flow → SUM
            total = sum(amounts)
        elif time_behavior == 2:  # Closing → last non-zero
            total = non_zero[-1] if non_zero else 0
        elif time_behavior == 3:  # Ratio → average of non-zero
            total = sum(non_zero) / len(non_zero) if non_zero else 0
        else:
            total = sum(amounts)

        total_updates.append({
            "metric_id": metric_id,
            "total_amount": total
        })

    return all_updates, total_updates, issues

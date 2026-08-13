import networkx as nx
from asteval import Interpreter
import re

def build_dependency_graph(rows: list[dict]) -> list[dict]:
    """
    Topological sort using networkx.
    Same logic as Kahn's algorithm we did in C#.
    Type = 1 → driver (no formula, no dependencies)
    Type = 2 → calculated (has formula, depends on other metrics)
    """
    graph = nx.DiGraph()

    # Add all nodes first
    for row in rows:
        graph.add_node(row["MetricCode"])

    # Add edges based on formula dependencies
    for row in rows:
        if row["Type"] == 2 and row["Formula"]:
            dependencies = extract_metric_codes(row["Formula"], rows)
            for dep in dependencies:
                # dep must be calculated before current row
                graph.add_edge(dep, row["MetricCode"])

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


def extract_metric_codes(formula: str, rows: list[dict]) -> list[str]:
    """
    Extract which tokens in the formula are MetricCodes (not numbers/operators).
    """
    all_metric_codes = {row["MetricCode"] for row in rows}

    # Tokenize formula - split by operators and spaces
    tokens = re.findall(r'[A-Za-z_][A-Za-z0-9_]*|[\d.]+', formula)

    dependencies = []
    for token in tokens:
        if token in all_metric_codes:
            dependencies.append(token)

    return dependencies


def resolve_formula(formula: str, metric_values: dict[str, float]) -> float:
    """
    Replace MetricCode tokens with their values and evaluate safely.
    
    Example:
        formula = "REVENUE - COST_OF_SALES"
        metric_values = { "REVENUE": 5000000, "COST_OF_SALES": 3200000 }
        result = 1800000
    """
    # Tokenize
    tokens = re.findall(r'[A-Za-z_][A-Za-z0-9_]*|[\d.]+|[+\-*/()%]', formula)

    resolved_tokens = []
    for token in tokens:
        if token in metric_values:
            # MetricCode → replace with numeric value
            value = metric_values[token]
            if value is None:
                raise ValueError(f"Metric '{token}' has no value (None). Cannot calculate.")
            resolved_tokens.append(str(value))
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
        raise ValueError(f"Formula evaluation error: {aeval.error_msg}")

    return float(result)


def is_number(token: str) -> bool:
    """Check if token is a pure numeric value."""
    try:
        float(token)
        return True
    except ValueError:
        return False


def calculate_all(rows: list[dict]) -> list[dict]:
    """
    Calculate per TimeId (1-12) independently.
    """
    from collections import defaultdict
    
    # Group rows by TimeId
    periods = defaultdict(list)
    for row in rows:
        periods[row["TimeId"]].append(row)

    all_updates = []
    metric_period_results = defaultdict(list)

    for time_id, period_rows in sorted(periods.items()):
        # Topological sort per period
        sorted_rows = build_dependency_graph(period_rows)

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
                    result = resolve_formula(row["Formula"], metric_values)
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
                except Exception as e:
                    raise Exception(f"[TimeId={time_id}] Error in '{row['MetricCode']}': {str(e)}")

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

    return all_updates, total_updates
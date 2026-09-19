"""
engine.py — executes an Agent Builder canvas graph.

A workflow is: {"nodes": [...], "edges": [...]}. Each node has:
  id, type ("trigger" | "agent" | "tool" | "condition" | "output"), config: {...}

Execution:
  1. Topologically sort nodes (Kahn's algorithm); reject cycles.
  2. Walk nodes in order. Each node's input is the joined output of its
     predecessors (trigger nodes use the run's initial input instead).
  3. trigger  -> passes input through unchanged
     agent    -> calls tools.gemini_agent
     tool     -> calls the configured tool in TOOL_REGISTRY, with the
                 input_field populated from upstream output
     condition-> evaluates config.expression against `output`; if false,
                 nothing downstream of this node receives its output
     output   -> just records the final value, no side effects
  4. Returns a step-by-step log plus the map of node_id -> result.
"""

import ast
import json
import time
from collections import deque

from tools import TOOL_REGISTRY, run_tool
from tools.gemini_agent import gemini_agent
from tools.autonomous_agent import autonomous_agent


class GraphError(Exception):
    pass


def _topo_order(nodes: list, edges: list) -> list:
    ids = [n["id"] for n in nodes]
    indeg = {i: 0 for i in ids}
    succ = {i: [] for i in ids}
    for e in edges:
        if e["from"] not in indeg or e["to"] not in indeg:
            continue
        succ[e["from"]].append(e["to"])
        indeg[e["to"]] += 1

    queue = deque([i for i in ids if indeg[i] == 0])
    order = []
    indeg = dict(indeg)
    while queue:
        nid = queue.popleft()
        order.append(nid)
        for s in succ[nid]:
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)

    if len(order) != len(ids):
        raise GraphError("The workflow has a cycle — execution requires a DAG.")
    return order


# ---- a small, safe evaluator for condition nodes -------------------------
# Supports: output, len(output), "x" in output, output.startswith/endswith,
# comparisons, and/or/not. No attribute access beyond the whitelisted
# string methods below, no calls other than len()/contains helpers.

_ALLOWED_STR_METHODS = {"startswith", "endswith", "lower", "upper", "strip"}


def _safe_eval(expr: str, output: str):
    tree = ast.parse(expr, mode="eval")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.BoolOp):
            vals = [ev(v) for v in node.values]
            return all(vals) if isinstance(node.op, ast.And) else any(vals)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not ev(node.operand)
        if isinstance(node, ast.Compare):
            left = ev(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = ev(comparator)
                if isinstance(op, ast.Eq):
                    ok = left == right
                elif isinstance(op, ast.NotEq):
                    ok = left != right
                elif isinstance(op, ast.Lt):
                    ok = left < right
                elif isinstance(op, ast.LtE):
                    ok = left <= right
                elif isinstance(op, ast.Gt):
                    ok = left > right
                elif isinstance(op, ast.GtE):
                    ok = left >= right
                elif isinstance(op, ast.In):
                    ok = left in right
                elif isinstance(op, ast.NotIn):
                    ok = left not in right
                else:
                    raise GraphError(f"Unsupported comparison in condition: {expr}")
                if not ok:
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "len" and len(node.args) == 1:
                return len(ev(node.args[0]))
            if isinstance(node.func, ast.Attribute) and node.func.attr in _ALLOWED_STR_METHODS:
                target = ev(node.func.value)
                args = [ev(a) for a in node.args]
                return getattr(target, node.func.attr)(*args)
            raise GraphError(f"Unsupported call in condition: {expr}")
        if isinstance(node, ast.Attribute):
            if node.attr in _ALLOWED_STR_METHODS:
                return getattr(ev(node.value), node.attr)
            raise GraphError(f"Unsupported attribute in condition: {expr}")
        if isinstance(node, ast.Name):
            if node.id == "output":
                return output
            raise GraphError(f"Unknown name in condition: {node.id}")
        if isinstance(node, ast.Constant):
            return node.value
        raise GraphError(f"Unsupported expression in condition: {expr}")

    return bool(ev(tree))


def run_workflow(nodes: list, edges: list, run_input: str = "") -> dict:
    node_by_id = {n["id"]: n for n in nodes}
    order = _topo_order(nodes, edges)

    preds = {n["id"]: [] for n in nodes}
    for e in edges:
        if e["to"] in preds:
            preds[e["to"]].append(e["from"])

    outputs = {}   # node_id -> str | None (None = blocked by a false condition)
    log = []

    for nid in order:
        node = node_by_id[nid]
        ntype = node.get("type")
        config = node.get("config") or {}

        upstream_vals = [outputs[p] for p in preds[nid] if outputs.get(p) is not None]
        combined_input = "\n\n".join(upstream_vals)

        started = time.time()
        try:
            if ntype == "trigger":
                result = run_input if run_input else config.get("initial_input", "")

            elif ntype == "condition":
                expr = (config.get("expression") or "true").strip()
                passed = _safe_eval(expr, combined_input)
                result = combined_input if passed else None
                log.append({
                    "node_id": nid, "type": ntype, "ok": True,
                    "detail": f"condition `{expr}` -> {passed}",
                    "elapsed_ms": int((time.time() - started) * 1000),
                })
                outputs[nid] = result
                continue

            elif ntype == "agent":
                params = {"prompt": config.get("prompt", ""), "input": combined_input,
                          "model": config.get("model", "gemini-2.5-flash")}
                result = gemini_agent(parameters=params)

            elif ntype == "autonomous":
                goal = config.get("goal") or combined_input
                auto_params = {
                    "goal": goal,
                    "model": config.get("model", "gemini-2.5-flash"),
                    "max_iterations": config.get("max_iterations", 6),
                    "allowed_tools": config.get("allowed_tools"),
                }
                run_result = autonomous_agent(parameters=auto_params)
                result = run_result.get("final_answer", "")

                trace_lines = []
                for step in run_result.get("transcript", []):
                    if "final_answer" in step:
                        trace_lines.append(f"[step {step['step']}] Final Answer: {step['final_answer']}")
                    elif "action" in step:
                        trace_lines.append(
                            f"[step {step['step']}] {step['action']}({json.dumps(step.get('action_input', {}))}) "
                            f"-> {(step.get('observation') or '')[:200]}"
                        )
                    elif "error" in step:
                        trace_lines.append(f"[step {step['step']}] error: {step['error']}")
                detail = result + ("\n\n--- reasoning trail ---\n" + "\n".join(trace_lines) if trace_lines else "")

                outputs[nid] = result
                log.append({
                    "node_id": nid, "type": ntype, "ok": True,
                    "detail": detail[:4000],
                    "elapsed_ms": int((time.time() - started) * 1000),
                })
                continue

            elif ntype == "tool":
                tool_id = config.get("tool")
                if not tool_id or tool_id not in TOOL_REGISTRY:
                    result = f"Node has no valid tool selected (got '{tool_id}')."
                else:
                    meta = TOOL_REGISTRY[tool_id]
                    params = dict(config.get("params") or {})
                    input_field = meta["input_field"]
                    if combined_input:
                        # don't clobber an explicit value the user typed for that field
                        params.setdefault(input_field, "")
                        if not params[input_field]:
                            params[input_field] = combined_input
                    result = run_tool(tool_id, params)


            elif ntype == "build_agent":
                from tools.agent_builder import build_agent
                params = {
                    "goal": config.get("goal", combined_input),
                    "model": config.get("model", "gemini-2.5-flash"),
                    "max_iterations": config.get("max_iterations", 10),
                    "output_path": config.get("output_path", ""),
                    "allowed_tools": config.get("allowed_tools"),
                }
                result = build_agent(parameters=params)
            elif ntype == "output":
                result = combined_input

            else:
                result = f"Unknown node type: {ntype}"

            outputs[nid] = result
            log.append({
                "node_id": nid, "type": ntype, "ok": True,
                "detail": (result or "")[:4000],
                "elapsed_ms": int((time.time() - started) * 1000),
            })

        except Exception as e:
            outputs[nid] = None
            log.append({
                "node_id": nid, "type": ntype, "ok": False,
                "detail": f"{type(e).__name__}: {e}",
                "elapsed_ms": int((time.time() - started) * 1000),
            })

    final_outputs = {
        nid: outputs.get(nid) for nid in node_by_id
        if node_by_id[nid].get("type") == "output"
    }

    return {"log": log, "node_outputs": outputs, "final_outputs": final_outputs}

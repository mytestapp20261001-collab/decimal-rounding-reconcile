#!/usr/bin/env python3
"""Offline, read-only audit. Python 3.12+, standard library only."""
import argparse
import json
import math
import re
import stat
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 50000
MAX_FINDINGS = 100

class InputError(ValueError):
    """A safe, non-payload-bearing input error code."""

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError("invalid_arguments")

def read_text(path):
    p = Path(path)
    if not stat.S_ISREG(p.stat().st_mode):
        raise InputError("regular_file_required")
    with p.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise InputError("file_size_limit")
    return data.decode("utf-8")

def reject_constant(value):
    raise InputError("non_finite_json_number")

def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("duplicate_json_member")
        result[key] = value
    return result

def parse_json(text):
    try:
        result = json.loads(text, object_pairs_hook=unique_pairs,
                            parse_float=Decimal, parse_constant=reject_constant)
    except InputError:
        raise
    except (ValueError, RecursionError, InvalidOperation):
        raise InputError("invalid_json") from None
    stack, nodes = [(result, 0)], 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > 128 or nodes > 200000:
            raise InputError("json_structure_limit")
        if isinstance(value, dict):
            stack.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            stack.extend((v, depth + 1) for v in value)
    return result

class Report:
    def __init__(self, tool):
        self.tool, self.findings, self.count = tool, [], 0
    def add(self, code, **location):
        self.count += 1
        if len(self.findings) < MAX_FINDINGS:
            self.findings.append(dict(code=code, **location))
    def finish(self, **summary):
        return {"tool": self.tool, "status": "fail" if self.count else "pass",
                "summary": dict(summary, finding_count=self.count),
                "findings": self.findings,
                "findings_truncated": self.count > len(self.findings)}

def emit_main(run):
    try:
        result = run()
        code = 1 if result["status"] == "fail" else 0
    except InputError as exc:
        result, code = {"tool": TOOL, "status": "error", "error": str(exc)}, 2
    except (OSError, UnicodeError):
        result, code = {"tool": TOOL, "status": "error", "error": "unreadable_utf8_input"}, 2
    except (ValueError, RecursionError, InvalidOperation):
        result, code = {"tool": TOOL, "status": "error", "error": "invalid_input"}, 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False))
    return code

TOOL = "decimal-rounding-reconcile"

import decimal

ROUNDINGS = {name: getattr(decimal, "ROUND_" + name.upper().replace("-", "_"))
             for name in ("half-even", "half-up", "half-down", "down", "up", "floor", "ceiling", "05up")}

def audit(values, *, places, rounding, show_totals=False):
    if type(places) is not int or not 0 <= places <= 12 or rounding not in ROUNDINGS:
        raise InputError("invalid_rounding_policy")
    if not isinstance(values, list) or not 1 <= len(values) <= 10000:
        raise InputError("nonempty_decimal_string_array_required")
    parsed = []
    for value in values:
        if (not isinstance(value, str) or len(value) > 128 or
            not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value)):
            raise InputError("invalid_decimal_string")
        try:
            number = Decimal(value)
        except decimal.InvalidOperation:
            raise InputError("invalid_decimal_string") from None
        if len(number.as_tuple().digits) > 100 or not -100 <= number.as_tuple().exponent <= 100 or number.adjusted() > 100:
            raise InputError("decimal_magnitude_limit")
        parsed.append(number)
    report = Report(TOOL)
    with decimal.localcontext() as context:
        context.prec = 256  # Exact sums under the documented magnitude/count bounds.
        quantum = Decimal(1).scaleb(-places)
        mode = ROUNDINGS[rounding]
        rounded = [n.quantize(quantum, rounding=mode) for n in parsed]
        raw_total = sum(parsed, Decimal(0))
        item_total = sum(rounded, Decimal(0)).quantize(quantum)
        total_rounded = raw_total.quantize(quantum, rounding=mode)
        difference = item_total - total_rounded
        if difference != 0:
            report.add("rounding_reconciliation_difference")
        result = report.finish(items=len(parsed), rounded_items=sum(a != b for a, b in zip(parsed, rounded)),
                               places=places, rounding=rounding, reconciled=difference == 0)
        if show_totals:
            result["totals"] = {"sum_of_rounded_items": format(item_total, "f"),
                                "rounded_sum": format(total_rounded, "f"), "difference": format(difference, "f")}
    return result

def run():
    parser = Parser(description=__doc__)
    parser.add_argument("input", help="JSON array of decimal strings")
    parser.add_argument("--places", type=int, required=True)
    parser.add_argument("--rounding", choices=tuple(ROUNDINGS), required=True)
    parser.add_argument("--show-totals", action="store_true", help="Opt in to derived monetary/numeric totals")
    args = parser.parse_args()
    return audit(parse_json(read_text(args.input)), places=args.places,
                 rounding=args.rounding, show_totals=args.show_totals)

if __name__ == "__main__":
    sys.exit(emit_main(run))

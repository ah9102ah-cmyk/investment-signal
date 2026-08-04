#!/usr/bin/env python3
"""Aggregate fund-holding and wallet data for fund-holding-list skill.

Input: JSON from stdin or --input. Expected shape:
{
  "wallet": <wallet API response or wrapper>,
  "funds": {
    "01": <fund holding API response or wrapper>,
    ...
  }
}

The script centralizes all summation used by the skill:
- wallet bankAccountShareList totalShare total
- fund summary totals from final display records:
  totalAmount, holdIncome, newestIncome
- top profit/loss sorting from final display records

Important: fund detail display fields are NOT recomputed by summing duplicate
records when an API-provided combined record is available. Final display records
are selected first, then totals are summed from those final records.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Tuple

SHARE_CATEGORIES = ["01", "02", "03", "04", "05", "06", "07"]
SUM_FIELDS = ["totalAmount", "holdIncome", "newestIncome"]
DISPLAY_FIELDS = ["totalAmount", "holdIncome", "holdIncomeRate", "holdVol", "newestIncome"]


def dec(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def money(value: Any) -> str:
    return str(dec(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def pct(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return f"{(dec(value) * Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"


def unwrap_response(obj: Any) -> Dict[str, Any]:
    """Accept either raw API response or wrapper {ok,status,json}."""
    if isinstance(obj, dict) and "json" in obj and isinstance(obj["json"], dict):
        return obj["json"]
    if isinstance(obj, dict):
        return obj
    return {}


def is_success(obj: Dict[str, Any]) -> bool:
    if not obj:
        return False
    if obj.get("status_code") == "0000":
        return True
    error = obj.get("error")
    return isinstance(error, dict) and error.get("id") == 0


def extract_wallet(raw: Dict[str, Any]) -> Dict[str, Any]:
    resp = unwrap_response(raw)
    if not is_success(resp):
        return {"ok": False, "data": None, "bank_total": "0.00"}
    data = resp.get("data") or {}
    bank_total = sum(dec(x.get("totalShare")) for x in data.get("bankAccountShareList") or [])
    return {"ok": True, "data": data, "bank_total": money(bank_total)}


def record_priority(record: Dict[str, Any]) -> Tuple[int, Decimal]:
    """Prefer API-provided combined records when duplicates exist.

    Known hints observed in API responses:
    - combineFlag often marks combined rows.
    - fundPositonCombinedList itself is already intended as combined data.

    If duplicates still appear, prefer records with combineFlag indicating a
    combined row, then the record with larger totalAmount as a deterministic
    fallback. This fallback only selects the final display record; it does not
    sum display fields across duplicate records.
    """
    combine_flag = str(record.get("combineFlag") or "")
    combined_score = 1 if combine_flag in {"1", "Y", "true", "True"} else 0
    return combined_score, dec(record.get("totalAmount"))


def select_final_fund_records(fund_responses: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    candidates_by_code: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    ok_categories: List[str] = []
    failed_categories: List[str] = []

    for category in SHARE_CATEGORIES:
        raw = fund_responses.get(category)
        resp = unwrap_response(raw)
        if not is_success(resp):
            failed_categories.append(category)
            continue
        ok_categories.append(category)
        data = resp.get("data") or {}
        for record in data.get("fundPositonCombinedList") or []:
            code = str(record.get("fundCode") or "")
            if not code:
                continue
            candidates_by_code.setdefault(code, []).append(record)

    final_records: List[Dict[str, Any]] = []
    for code, records in candidates_by_code.items():
        # If there is one record, use it directly. If there are multiple, select
        # the best API-provided combined record; do not sum display fields.
        chosen = max(records, key=record_priority)
        normalized = dict(chosen)
        normalized["duplicateRecordCount"] = len(records)
        normalized["displayFieldsSource"] = "api_combined_record" if len(records) > 1 else "api_record"
        final_records.append(normalized)

    final_records.sort(key=lambda r: dec(r.get("totalAmount")), reverse=True)
    return final_records, ok_categories, failed_categories


def summarize_funds(records: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    records = list(records)
    return {
        "fundCount": str(len(records)),
        "totalAmount": money(sum(dec(r.get("totalAmount")) for r in records)),
        "holdIncome": money(sum(dec(r.get("holdIncome")) for r in records)),
        "newestIncome": money(sum(dec(r.get("newestIncome")) for r in records)),
    }


def normalize_fund_record(record: Dict[str, Any]) -> Dict[str, Any]:
    hold_vol = dec(record.get("holdVol"))
    total_amount = dec(record.get("totalAmount"))
    return {
        "fundCode": record.get("fundCode"),
        "fundName": record.get("fundName"),
        "totalAmount": money(record.get("totalAmount")),
        "holdIncome": money(record.get("holdIncome")),
        "holdIncomeRate": pct(record.get("holdIncomeRate")),
        "holdVol": "待确认" if hold_vol == 0 and total_amount > 0 else money(record.get("holdVol")),
        "newestIncome": money(record.get("newestIncome")),
        "duplicateRecordCount": record.get("duplicateRecordCount", 1),
        "displayFieldsSource": record.get("displayFieldsSource", "api_record"),
        "raw": {field: record.get(field) for field in DISPLAY_FIELDS},
    }


def top_records(records: List[Dict[str, Any]], reverse: bool) -> List[Dict[str, Any]]:
    if reverse:
        eligible = [r for r in records if dec(r.get("holdIncome")) > 0]
    else:
        eligible = [r for r in records if dec(r.get("holdIncome")) < 0]
    selected = sorted(eligible, key=lambda r: dec(r.get("holdIncome")), reverse=reverse)[:5]
    return [
        {
            "fundCode": r.get("fundCode"),
            "fundName": r.get("fundName"),
            "holdIncome": money(r.get("holdIncome")),
            "holdIncomeRate": pct(r.get("holdIncomeRate")),
        }
        for r in selected
    ]


def aggregate(payload: Dict[str, Any]) -> Dict[str, Any]:
    wallet = extract_wallet(payload.get("wallet") or {})
    final_records, ok_categories, failed_categories = select_final_fund_records(payload.get("funds") or {})
    return {
        "wallet": wallet,
        "fundApi": {
            "okCategories": ok_categories,
            "failedCategories": failed_categories,
        },
        "fundSummary": summarize_funds(final_records),
        "funds": [normalize_fund_record(r) for r in final_records],
        "topProfitFunds": top_records(final_records, reverse=True),
        "topLossFunds": top_records(final_records, reverse=False),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate fund holding data for fund-holding-list skill")
    parser.add_argument("--input", "-i", help="Input JSON file. Defaults to stdin.")
    parser.add_argument("--output", "-o", help="Output JSON file. Defaults to stdout.")
    args = parser.parse_args()

    try:
        if args.input:
            with open(args.input, "r", encoding="utf-8") as f:
                payload = json.load(f)
        else:
            payload = json.load(sys.stdin)
    except Exception as exc:
        print(f"Failed to read input JSON: {exc}", file=sys.stderr)
        return 2

    result = aggregate(payload)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output + "\n")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
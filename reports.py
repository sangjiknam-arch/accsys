# -*- coding: utf-8 -*-
from datetime import date

from database import get_connection


def default_fiscal_period(today=None):
    today = today or date.today()
    start_year = today.year if today.month == 12 else today.year - 1
    return f"{start_year:04d}-12-01", today.isoformat()


def fiscal_start_for(as_of_text):
    d = date.fromisoformat(as_of_text)
    start_year = d.year if d.month == 12 else d.year - 1
    return f"{start_year:04d}-12-01"


def fiscal_year_end(today=None):
    today = today or date.today()
    end_year = today.year + 1 if today.month == 12 else today.year
    return f"{end_year:04d}-11-30"


def fetch_trial_balance(start_text, end_text, include_zero=False):
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                a.code,
                a.name,
                a.type,
                COALESCE(SUM(CASE
                    WHEN jm.journal_date BETWEEN ? AND ? THEN jd.debit
                    ELSE 0 END), 0) AS debit_total,
                COALESCE(SUM(CASE
                    WHEN jm.journal_date BETWEEN ? AND ? THEN jd.credit
                    ELSE 0 END), 0) AS credit_total
            FROM accounts a
            LEFT JOIN journal_detail jd ON a.code = jd.account_code
            LEFT JOIN journal_master jm ON jd.journal_no = jm.journal_no
            GROUP BY a.code, a.name, a.type
            ORDER BY
                CASE a.type
                    WHEN '자산' THEN 1
                    WHEN '부채' THEN 2
                    WHEN '순자산' THEN 3
                    WHEN '수입' THEN 4
                    WHEN '지출' THEN 5
                    ELSE 6
                END,
                a.code
            """,
            (start_text, end_text, start_text, end_text),
        ).fetchall()

        display = []
        total_debit = total_credit = 0
        total_debit_balance = total_credit_balance = 0

        for row in rows:
            debit_total = int(row["debit_total"] or 0)
            credit_total = int(row["credit_total"] or 0)
            if not include_zero and debit_total == 0 and credit_total == 0:
                continue

            difference = debit_total - credit_total
            debit_balance = max(difference, 0)
            credit_balance = max(-difference, 0)

            display.append({
                "debit_balance": debit_balance,
                "debit_total": debit_total,
                "code": row["code"],
                "name": row["name"],
                "type": row["type"],
                "credit_total": credit_total,
                "credit_balance": credit_balance,
            })
            total_debit += debit_total
            total_credit += credit_total
            total_debit_balance += debit_balance
            total_credit_balance += credit_balance

        return {
            "start": start_text,
            "end": end_text,
            "include_zero": include_zero,
            "rows": display,
            "total_debit": total_debit,
            "total_credit": total_credit,
            "total_debit_balance": total_debit_balance,
            "total_credit_balance": total_credit_balance,
            "difference": total_debit - total_credit,
        }
    finally:
        conn.close()


def fetch_income_statement(start_text, end_text):
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                a.code,
                a.name,
                a.type,
                COALESCE(SUM(CASE
                    WHEN jm.journal_date BETWEEN ? AND ? THEN jd.debit
                    ELSE 0 END), 0) AS debit_total,
                COALESCE(SUM(CASE
                    WHEN jm.journal_date BETWEEN ? AND ? THEN jd.credit
                    ELSE 0 END), 0) AS credit_total
            FROM accounts a
            LEFT JOIN journal_detail jd ON a.code = jd.account_code
            LEFT JOIN journal_master jm ON jd.journal_no = jm.journal_no
            WHERE a.type IN ('수입', '지출')
            GROUP BY a.code, a.name, a.type
            ORDER BY CASE a.type WHEN '수입' THEN 1 WHEN '지출' THEN 2 ELSE 3 END, a.code
            """,
            (start_text, end_text, start_text, end_text),
        ).fetchall()

        display = []
        total_income = 0
        total_expense = 0

        for row in rows:
            debit_total = int(row["debit_total"] or 0)
            credit_total = int(row["credit_total"] or 0)
            account_type = row["type"]
            amount = credit_total - debit_total if account_type == "수입" else debit_total - credit_total
            if amount == 0:
                continue

            debit_display = credit_display = 0
            if account_type == "수입":
                if amount >= 0:
                    credit_display = amount
                else:
                    debit_display = abs(amount)
                total_income += amount
            else:
                if amount >= 0:
                    debit_display = amount
                else:
                    credit_display = abs(amount)
                total_expense += amount

            display.append({
                "type": account_type,
                "code": row["code"],
                "name": row["name"],
                "debit": debit_display,
                "credit": credit_display,
                "balance": abs(amount),
            })

        net_income = total_income - total_expense
        if net_income > 0:
            display.append({
                "type": "당기순손익", "code": "자동", "name": "당기순이익",
                "debit": net_income, "credit": 0, "balance": net_income,
            })
            result_text = "당기순이익"
        elif net_income < 0:
            loss = abs(net_income)
            display.append({
                "type": "당기순손익", "code": "자동", "name": "당기순손실",
                "debit": 0, "credit": loss, "balance": loss,
            })
            result_text = "당기순손실"
        else:
            result_text = "수입·지출 일치"

        return {
            "start": start_text,
            "end": end_text,
            "rows": display,
            "total_income": total_income,
            "total_expense": total_expense,
            "net_income": net_income,
            "result_text": result_text,
        }
    finally:
        conn.close()


def _calculate_current_result(conn, as_of_text):
    fiscal_start = fiscal_start_for(as_of_text)
    rows = conn.execute(
        """
        SELECT
            a.type,
            COALESCE(SUM(CASE
                WHEN jm.journal_date BETWEEN ? AND ? THEN jd.debit
                ELSE 0 END), 0) AS debit_total,
            COALESCE(SUM(CASE
                WHEN jm.journal_date BETWEEN ? AND ? THEN jd.credit
                ELSE 0 END), 0) AS credit_total
        FROM accounts a
        LEFT JOIN journal_detail jd ON a.code = jd.account_code
        LEFT JOIN journal_master jm ON jd.journal_no = jm.journal_no
        WHERE a.type IN ('수입', '지출')
        GROUP BY a.type
        """,
        (fiscal_start, as_of_text, fiscal_start, as_of_text),
    ).fetchall()

    total_income = total_expense = 0
    for row in rows:
        debit_total = int(row["debit_total"] or 0)
        credit_total = int(row["credit_total"] or 0)
        if row["type"] == "수입":
            total_income += credit_total - debit_total
        elif row["type"] == "지출":
            total_expense += debit_total - credit_total
    return total_income - total_expense


def fetch_balance_sheet(as_of_text):
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT
                a.code,
                a.name,
                a.type,
                COALESCE(SUM(CASE WHEN jm.journal_date <= ? THEN jd.debit ELSE 0 END), 0) AS debit_total,
                COALESCE(SUM(CASE WHEN jm.journal_date <= ? THEN jd.credit ELSE 0 END), 0) AS credit_total
            FROM accounts a
            LEFT JOIN journal_detail jd ON a.code = jd.account_code
            LEFT JOIN journal_master jm ON jd.journal_no = jm.journal_no
            WHERE
                a.type IN ('자산', '부채', '순자산')
                OR (
                    TRIM(COALESCE(a.type, '')) = ''
                    AND (
                        a.code IN ('000', '901')
                        OR a.name IN ('전기이월', '당기순이익', '당기순손실')
                    )
                )
            GROUP BY a.code, a.name, a.type
            ORDER BY
                CASE
                    WHEN a.type = '자산' THEN 1
                    WHEN a.type = '부채' THEN 2
                    WHEN a.type = '순자산' THEN 3
                    WHEN TRIM(COALESCE(a.type, '')) = ''
                         AND (a.code IN ('000', '901') OR a.name IN ('전기이월', '당기순이익', '당기순손실')) THEN 3
                    ELSE 4
                END,
                a.code
            """,
            (as_of_text, as_of_text),
        ).fetchall()

        display = []
        total_debit = total_credit = 0

        for row in rows:
            account_code = row["code"]
            account_name = row["name"]
            account_type = row["type"]
            debit_total = int(row["debit_total"] or 0)
            credit_total = int(row["credit_total"] or 0)

            effective_type = account_type
            if not str(account_type or "").strip():
                if str(account_code) in ("000", "901") or str(account_name) in ("전기이월", "당기순이익", "당기순손실"):
                    effective_type = "순자산"

            if effective_type == "자산":
                balance = debit_total - credit_total
                normal_side = "차변"
            else:
                balance = credit_total - debit_total
                normal_side = "대변"

            if balance == 0:
                continue

            if balance > 0:
                balance_side = normal_side
                debit_display = balance if normal_side == "차변" else 0
                credit_display = balance if normal_side == "대변" else 0
            else:
                balance_side = "대변" if normal_side == "차변" else "차변"
                debit_display = abs(balance) if balance_side == "차변" else 0
                credit_display = abs(balance) if balance_side == "대변" else 0

            display_name = account_name
            if effective_type == "순자산" and not str(account_type or "").strip() and str(account_name) in ("당기순이익", "당기순손실"):
                display_name = f"전기이월({account_name})"

            display.append({
                "type": effective_type,
                "code": account_code,
                "name": display_name,
                "debit": debit_display,
                "credit": credit_display,
                "balance": abs(balance),
                "balance_side": balance_side,
            })
            total_debit += debit_display
            total_credit += credit_display

        current_result = _calculate_current_result(conn, as_of_text)
        if current_result > 0:
            display.append({
                "type": "순자산", "code": "자동", "name": "당기순이익",
                "debit": 0, "credit": current_result, "balance": current_result,
                "balance_side": "대변",
            })
            total_credit += current_result
        elif current_result < 0:
            loss = abs(current_result)
            display.append({
                "type": "순자산", "code": "자동", "name": "당기순손실",
                "debit": loss, "credit": 0, "balance": loss,
                "balance_side": "차변",
            })
            total_debit += loss

        difference = total_debit - total_credit
        return {
            "as_of": as_of_text,
            "rows": display,
            "total_debit": total_debit,
            "total_credit": total_credit,
            "difference": difference,
            "current_result": current_result,
            "status": "차변·대변 일치" if difference == 0 else "불일치 확인 필요",
        }
    finally:
        conn.close()

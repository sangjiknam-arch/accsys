# -*- coding: utf-8 -*-
import os
from datetime import date
from functools import wraps

from flask import Flask, flash, redirect, render_template, request, session, url_for

from database import authenticate_user, change_user_password, create_tables, get_connection
from extra_routes import register_extra_routes

from reports import (
    default_fiscal_period as report_default_fiscal_period,
    fetch_balance_sheet,
    fetch_income_statement,
    fetch_trial_balance,
    fiscal_year_end,
)

CASH_ACCOUNT_CODE = "1010"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "DEV-ONLY-change-this-secret-key")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            flash("로그인이 필요합니다.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def db_rows(sql, params=()):
    conn = get_connection()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def db_row(sql, params=()):
    conn = get_connection()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def parse_amount(value):
    text = str(value or "").replace(",", "").strip()
    if not text or not text.isdigit():
        raise ValueError("금액은 1 이상의 숫자로 입력하십시오.")
    amount = int(text)
    if amount <= 0:
        raise ValueError("금액은 0보다 커야 합니다.")
    return amount


def ensure_cash_account(conn):
    row = conn.execute("SELECT code FROM accounts WHERE code = ?", (CASH_ACCOUNT_CODE,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO accounts (code, name, type) VALUES (?, ?, ?)",
            (CASH_ACCOUNT_CODE, "현금", "자산"),
        )


def generate_journal_no(conn, prefix, journal_date):
    date_part = journal_date.replace("-", "")
    pattern = f"{prefix}{date_part}%"
    row = conn.execute(
        "SELECT MAX(journal_no) AS last_no FROM journal_master WHERE journal_no LIKE ?",
        (pattern,),
    ).fetchone()
    last_no = row["last_no"] if row else None
    sequence = 1
    if last_no:
        try:
            sequence = int(str(last_no)[-4:]) + 1
        except (ValueError, TypeError):
            sequence = 1
    return f"{prefix}{date_part}{sequence:04d}"


def load_reference_data():
    members = db_rows("""
        SELECT member_no, name, COALESCE(position, '') AS position
        FROM members
        ORDER BY name, member_no
    """)
    income_accounts = db_rows("""
        SELECT code, name FROM accounts WHERE type = '수입' ORDER BY code
    """)
    expense_accounts = db_rows("""
        SELECT code, name FROM accounts WHERE type = '지출' ORDER BY code
    """)
    return members, income_accounts, expense_accounts


def load_income_records(limit=200):
    return db_rows("""
        SELECT jm.journal_no, jm.journal_date,
               COALESCE(m.name, '') AS member_name,
               COALESCE(a.name, '') AS account_name,
               COALESCE(jd.credit, 0) AS amount,
               COALESCE(jm.summary, '') AS summary
        FROM journal_master jm
        JOIN journal_detail jd
          ON jm.journal_no = jd.journal_no
         AND jd.account_code = jm.offering_code
        LEFT JOIN members m ON jm.member_no = m.member_no
        LEFT JOIN accounts a ON jm.offering_code = a.code
        WHERE jm.offering_code IS NOT NULL
          AND COALESCE(jd.credit, 0) > 0
        ORDER BY jm.journal_date DESC, jm.journal_no DESC
        LIMIT ?
    """, (limit,))


def load_expense_records(limit=200):
    return db_rows("""
        SELECT jm.journal_no, jm.journal_date,
               COALESCE(a.name, '') AS account_name,
               COALESCE(jd.debit, 0) AS amount,
               COALESCE(jm.summary, '') AS summary
        FROM journal_master jm
        JOIN journal_detail jd ON jm.journal_no = jd.journal_no
        JOIN accounts a ON jd.account_code = a.code
        WHERE jm.offering_code IS NULL
          AND a.type = '지출'
          AND COALESCE(jd.debit, 0) > 0
        ORDER BY jm.journal_date DESC, jm.journal_no DESC
        LIMIT ?
    """, (limit,))


def journal_summary():
    conn = get_connection()
    try:
        income = conn.execute("""
            SELECT COALESCE(SUM(jd.credit), 0) AS total
            FROM journal_master jm
            JOIN journal_detail jd
              ON jm.journal_no = jd.journal_no
             AND jd.account_code = jm.offering_code
            WHERE jm.offering_code IS NOT NULL
        """).fetchone()["total"] or 0
        expense = conn.execute("""
            SELECT COALESCE(SUM(jd.debit), 0) AS total
            FROM journal_master jm
            JOIN journal_detail jd ON jm.journal_no = jd.journal_no
            JOIN accounts a ON jd.account_code = a.code
            WHERE jm.offering_code IS NULL AND a.type = '지출'
        """).fetchone()["total"] or 0
        return int(round(income)), int(round(expense))
    finally:
        conn.close()




# =========================================================
# 현금출납장
# =========================================================
def fiscal_year_start(start_text):
    d = date.fromisoformat(start_text)
    year = d.year if d.month == 12 else d.year - 1
    return f"{year:04d}-12-01"


def default_fiscal_period():
    today = date.today()
    start_year = today.year if today.month == 12 else today.year - 1
    return f"{start_year:04d}-12-01", today.isoformat()


def cashbook_opening_balance(conn, start_text):
    fiscal_start = fiscal_year_start(start_text)
    row = conn.execute("""
        SELECT
            COALESCE(SUM(jd.debit), 0) AS debit_total,
            COALESCE(SUM(jd.credit), 0) AS credit_total
        FROM journal_detail jd
        INNER JOIN journal_master jm
            ON jd.journal_no = jm.journal_no
        WHERE jd.account_code = ?
          AND (
                jm.journal_no LIKE 'I%'
                OR jm.journal_no LIKE 'E%'
                OR jm.journal_no LIKE 'GJ%'
          )
          AND jm.journal_date >= ?
          AND (
                jm.journal_date < ?
                OR (
                    jm.journal_date = ?
                    AND (
                        TRIM(COALESCE(jm.summary, '')) LIKE '%기초잔액%'
                        OR TRIM(COALESCE(jm.summary, '')) LIKE '%전기이월%'
                        OR TRIM(COALESCE(jm.summary, '')) LIKE '%기초이월%'
                        OR EXISTS (
                            SELECT 1
                            FROM journal_detail opening_detail
                            WHERE opening_detail.journal_no = jm.journal_no
                              AND (
                                  TRIM(COALESCE(opening_detail.remark, '')) LIKE '%기초잔액%'
                                  OR TRIM(COALESCE(opening_detail.remark, '')) LIKE '%전기이월%'
                                  OR TRIM(COALESCE(opening_detail.remark, '')) LIKE '%기초이월%'
                              )
                        )
                    )
                )
          )
    """, (CASH_ACCOUNT_CODE, fiscal_start, start_text, start_text)).fetchone()

    return int(row["debit_total"] or 0) - int(row["credit_total"] or 0)


def fetch_cashbook(start_text, end_text):
    conn = get_connection()
    try:
        ensure_cash_account(conn)
        opening = cashbook_opening_balance(conn, start_text)

        rows = conn.execute("""
            WITH cash_rows AS (
                SELECT
                    jm.journal_no,
                    jm.journal_date,
                    COALESCE(jm.summary, '') AS master_summary,
                    SUM(COALESCE(jd.debit, 0)) AS cash_debit,
                    SUM(COALESCE(jd.credit, 0)) AS cash_credit,
                    GROUP_CONCAT(
                        DISTINCT NULLIF(TRIM(jd.remark), '')
                    ) AS cash_remarks
                FROM journal_master jm
                INNER JOIN journal_detail jd
                    ON jm.journal_no = jd.journal_no
                WHERE jd.account_code = ?
                  AND (
                        jm.journal_no LIKE 'I%'
                        OR jm.journal_no LIKE 'E%'
                        OR jm.journal_no LIKE 'GJ%'
                  )
                  AND jm.journal_date BETWEEN ? AND ?
                  AND NOT (
                      TRIM(COALESCE(jm.summary, '')) LIKE '%기초잔액%'
                      OR TRIM(COALESCE(jm.summary, '')) LIKE '%전기이월%'
                      OR TRIM(COALESCE(jm.summary, '')) LIKE '%기초이월%'
                      OR EXISTS (
                          SELECT 1
                          FROM journal_detail opening_detail
                          WHERE opening_detail.journal_no = jm.journal_no
                            AND (
                                TRIM(COALESCE(opening_detail.remark, '')) LIKE '%기초잔액%'
                                OR TRIM(COALESCE(opening_detail.remark, '')) LIKE '%전기이월%'
                                OR TRIM(COALESCE(opening_detail.remark, '')) LIKE '%기초이월%'
                            )
                      )
                  )
                GROUP BY jm.journal_no, jm.journal_date, jm.summary
                HAVING SUM(COALESCE(jd.debit, 0)) <> 0
                    OR SUM(COALESCE(jd.credit, 0)) <> 0
            ),
            cash_with_counter AS (
                SELECT
                    cr.journal_no,
                    cr.journal_date,
                    CASE
                        WHEN cr.cash_debit > cr.cash_credit THEN '수입'
                        ELSE '지출'
                    END AS transaction_type,
                    COALESCE((
                        SELECT GROUP_CONCAT(account_name, ', ')
                        FROM (
                            SELECT DISTINCT COALESCE(a.name, d.account_code) AS account_name
                            FROM journal_detail d
                            LEFT JOIN accounts a ON a.code = d.account_code
                            WHERE d.journal_no = cr.journal_no
                              AND d.account_code <> ?
                            ORDER BY d.account_code
                        )
                    ), '') AS counter_account,
                    COALESCE(
                        NULLIF(TRIM(cr.cash_remarks), ''),
                        NULLIF(TRIM(cr.master_summary), ''),
                        ''
                    ) AS remark,
                    CASE
                        WHEN cr.cash_debit > cr.cash_credit
                            THEN cr.cash_debit - cr.cash_credit
                        ELSE 0
                    END AS income_amount,
                    CASE
                        WHEN cr.cash_credit > cr.cash_debit
                            THEN cr.cash_credit - cr.cash_debit
                        ELSE 0
                    END AS expense_amount
                FROM cash_rows cr
            )
            SELECT
                journal_date,
                transaction_type,
                counter_account,
                GROUP_CONCAT(DISTINCT NULLIF(TRIM(remark), '')) AS remarks,
                COUNT(DISTINCT journal_no) AS voucher_count,
                SUM(income_amount) AS income_amount,
                SUM(expense_amount) AS expense_amount
            FROM cash_with_counter
            GROUP BY journal_date, transaction_type, counter_account
            ORDER BY
                date(journal_date) ASC,
                CASE transaction_type WHEN '수입' THEN 1 ELSE 2 END,
                counter_account ASC
        """, (CASH_ACCOUNT_CODE, start_text, end_text, CASH_ACCOUNT_CODE)).fetchall()

        running = opening
        total_income = opening
        total_expense = 0
        result_rows = []

        for row in rows:
            income = int(row["income_amount"] or 0)
            expense = int(row["expense_amount"] or 0)
            count = int(row["voucher_count"] or 0)
            running += income - expense
            total_income += income
            total_expense += expense

            remark_list = [
                p.strip() for p in str(row["remarks"] or "").split(",")
                if p.strip()
            ]
            if not remark_list:
                remark = f"{count}건 합산" if count > 1 else ""
            elif count <= 1:
                remark = remark_list[0]
            else:
                remark = f"{remark_list[0]} 외 {count - 1}건"

            result_rows.append({
                "date": row["journal_date"] or "",
                "type": row["transaction_type"] or "",
                "account": row["counter_account"] or "",
                "remark": remark,
                "income": income,
                "expense": expense,
                "balance": running,
            })

        return {
            "start": start_text,
            "end": end_text,
            "opening": opening,
            "rows": result_rows,
            "total_income": total_income,
            "total_expense": total_expense,
            "closing": running,
        }
    finally:
        conn.close()


# =========================================================
# 총계정원장
# =========================================================
def is_debit_normal_type(account_type):
    return account_type in ("자산", "지출")


def calculate_balance_for_type(account_type, debit_total, credit_total):
    if is_debit_normal_type(account_type):
        return debit_total - credit_total
    return credit_total - debit_total


def balance_side_for_type(account_type, balance):
    if balance == 0:
        return "-"
    if is_debit_normal_type(account_type):
        return "차변" if balance >= 0 else "대변"
    return "대변" if balance >= 0 else "차변"


def ledger_accounts():
    return db_rows("""
        SELECT code, name, type
        FROM accounts
        ORDER BY code
    """)


def fetch_account_ledger(conn, account, start_text, end_text):
    opening = conn.execute("""
        SELECT
            COALESCE(SUM(jd.debit), 0) AS debit_total,
            COALESCE(SUM(jd.credit), 0) AS credit_total
        FROM journal_detail jd
        INNER JOIN journal_master jm
            ON jd.journal_no = jm.journal_no
        WHERE jd.account_code = ?
          AND jm.journal_date < ?
    """, (account["code"], start_text)).fetchone()

    opening_debit = float(opening["debit_total"] or 0)
    opening_credit = float(opening["credit_total"] or 0)
    opening_balance = calculate_balance_for_type(
        account["type"], opening_debit, opening_credit
    )

    source_rows = conn.execute("""
        SELECT
            jm.journal_date,
            jm.journal_no,
            COALESCE(
                (
                    SELECT GROUP_CONCAT(oa.name, ', ')
                    FROM journal_detail od
                    INNER JOIN accounts oa ON od.account_code = oa.code
                    WHERE od.journal_no = jd.journal_no
                      AND od.id <> jd.id
                ),
                ''
            ) AS counter_account,
            COALESCE(NULLIF(jd.remark, ''), jm.summary, '') AS remark,
            COALESCE(jd.debit, 0) AS debit,
            COALESCE(jd.credit, 0) AS credit,
            jd.id
        FROM journal_detail jd
        INNER JOIN journal_master jm ON jd.journal_no = jm.journal_no
        WHERE jd.account_code = ?
          AND jm.journal_date BETWEEN ? AND ?
        ORDER BY jm.journal_date ASC, jm.journal_no ASC, jd.id ASC
    """, (account["code"], start_text, end_text)).fetchall()

    running = opening_balance
    total_debit = 0.0
    total_credit = 0.0
    rows = []

    for row in source_rows:
        debit = float(row["debit"] or 0)
        credit = float(row["credit"] or 0)
        total_debit += debit
        total_credit += credit
        if is_debit_normal_type(account["type"]):
            running += debit - credit
        else:
            running += credit - debit

        rows.append({
            "date": row["journal_date"] or "",
            "journal_no": row["journal_no"] or "",
            "counter_account": row["counter_account"] or "",
            "remark": row["remark"] or "",
            "debit": debit,
            "credit": credit,
            "balance": running,
            "balance_side": balance_side_for_type(account["type"], running),
        })

    return {
        "account": dict(account),
        "opening_balance": opening_balance,
        "opening_side": balance_side_for_type(account["type"], opening_balance),
        "total_debit": total_debit,
        "total_credit": total_credit,
        "closing_balance": running,
        "closing_side": balance_side_for_type(account["type"], running),
        "rows": rows,
    }


def fetch_all_ledgers(start_text, end_text):
    conn = get_connection()
    try:
        result = []
        for account in ledger_accounts():
            ledger = fetch_account_ledger(conn, account, start_text, end_text)
            if ledger["rows"] or ledger["opening_balance"] != 0:
                result.append(ledger)
        return result
    finally:
        conn.close()


@app.route("/")
def index():
    return redirect(url_for("dashboard" if "user" in session else "login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        userid = request.form.get("userid", "").strip()
        password = request.form.get("password", "")
        user = authenticate_user(userid, password)
        if user:
            session.clear()
            session["user"] = dict(user)

            # 최초 관리자 비밀번호로 로그인한 경우 즉시 변경 화면으로 이동
            if user.get("role") == "admin" and password == "12345678":
                flash("보안을 위해 최초 비밀번호를 새 8자리 숫자로 변경하십시오.", "warning")
                return redirect(url_for("change_password"))

            return redirect(url_for("dashboard"))
        flash("아이디 또는 비밀번호가 올바르지 않습니다.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("로그아웃되었습니다.", "success")
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if new_password != confirm_password:
            flash("새 비밀번호와 확인 비밀번호가 일치하지 않습니다.", "danger")
            return render_template("change_password.html")

        if not (len(new_password) == 8 and new_password.isdigit()):
            flash("새 비밀번호는 숫자 정확히 8자리여야 합니다.", "danger")
            return render_template("change_password.html")

        success, message = change_user_password(
            userid=session["user"]["userid"],
            current_password=current_password,
            new_password=new_password,
        )

        if success:
            flash(message, "success")
            return redirect(url_for("dashboard"))

        flash(message, "danger")

    return render_template("change_password.html")


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/journal")
@login_required
def journal():
    members, income_accounts, expense_accounts = load_reference_data()
    incomes = load_income_records()
    expenses = load_expense_records()
    total_income, total_expense = journal_summary()
    return render_template(
        "journal.html",
        today=date.today().isoformat(),
        members=members,
        income_accounts=income_accounts,
        expense_accounts=expense_accounts,
        incomes=incomes,
        expenses=expenses,
        total_income=total_income,
        total_expense=total_expense,
        difference=total_income-total_expense,
    )


@app.post("/journal/income/save")
@login_required
def save_income():
    try:
        journal_date = request.form.get("journal_date", "").strip()
        member_no = int(request.form.get("member_no", ""))
        offering_code = request.form.get("offering_code", "").strip()
        amount = parse_amount(request.form.get("amount"))
        summary = request.form.get("summary", "").strip() or "헌금 수입"
        if not journal_date or not offering_code:
            raise ValueError("헌금일과 헌금종류를 선택하십시오.")

        conn = get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            ensure_cash_account(conn)
            if conn.execute("SELECT 1 FROM members WHERE member_no=?", (member_no,)).fetchone() is None:
                raise ValueError("등록된 헌금자를 선택하십시오.")
            if conn.execute("SELECT 1 FROM accounts WHERE code=? AND type='수입'", (offering_code,)).fetchone() is None:
                raise ValueError("등록된 수입 계정과목을 선택하십시오.")

            journal_no = generate_journal_no(conn, "I", journal_date)
            conn.execute("""
                INSERT INTO journal_master
                (journal_no, journal_date, member_no, offering_code, summary, input_user)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (journal_no, journal_date, member_no, offering_code, summary, session["user"]["userid"]))
            conn.executemany("""
                INSERT INTO journal_detail
                (journal_no, account_code, debit, credit, remark)
                VALUES (?, ?, ?, ?, ?)
            """, [
                (journal_no, CASH_ACCOUNT_CODE, amount, 0, summary),
                (journal_no, offering_code, 0, amount, summary),
            ])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        flash(f"수입 전표 {journal_no}가 저장되었습니다.", "success")
    except Exception as error:
        flash(str(error), "danger")
    return redirect(url_for("journal"))


@app.post("/journal/expense/save")
@login_required
def save_expense():
    try:
        journal_date = request.form.get("journal_date", "").strip()
        expense_code = request.form.get("expense_code", "").strip()
        amount = parse_amount(request.form.get("amount"))
        summary = request.form.get("summary", "").strip() or "현금 지출"
        if not journal_date or not expense_code:
            raise ValueError("지출일과 지출항목을 선택하십시오.")

        conn = get_connection()
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            ensure_cash_account(conn)
            if conn.execute("SELECT 1 FROM accounts WHERE code=? AND type='지출'", (expense_code,)).fetchone() is None:
                raise ValueError("등록된 지출 계정과목을 선택하십시오.")

            journal_no = generate_journal_no(conn, "E", journal_date)
            conn.execute("""
                INSERT INTO journal_master
                (journal_no, journal_date, member_no, offering_code, summary, input_user)
                VALUES (?, ?, NULL, NULL, ?, ?)
            """, (journal_no, journal_date, summary, session["user"]["userid"]))
            conn.executemany("""
                INSERT INTO journal_detail
                (journal_no, account_code, debit, credit, remark)
                VALUES (?, ?, ?, ?, ?)
            """, [
                (journal_no, expense_code, amount, 0, summary),
                (journal_no, CASH_ACCOUNT_CODE, 0, amount, summary),
            ])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        flash(f"지출 전표 {journal_no}가 저장되었습니다.", "success")
    except Exception as error:
        flash(str(error), "danger")
    return redirect(url_for("journal"))


@app.route("/journal/income/<journal_no>/edit", methods=["GET", "POST"])
@login_required
def edit_income(journal_no):
    conn = get_connection()
    try:
        if request.method == "POST":
            journal_date = request.form.get("journal_date", "").strip()
            member_no = int(request.form.get("member_no", ""))
            offering_code = request.form.get("offering_code", "").strip()
            amount = parse_amount(request.form.get("amount"))
            summary = request.form.get("summary", "").strip() or "헌금 수입"

            ensure_cash_account(conn)
            conn.execute("""
                UPDATE journal_master
                SET journal_date=?, member_no=?, offering_code=?, summary=?, input_user=?
                WHERE journal_no=?
            """, (journal_date, member_no, offering_code, summary, session["user"]["userid"], journal_no))
            conn.execute("DELETE FROM journal_detail WHERE journal_no=?", (journal_no,))
            conn.executemany("""
                INSERT INTO journal_detail
                (journal_no, account_code, debit, credit, remark)
                VALUES (?, ?, ?, ?, ?)
            """, [
                (journal_no, CASH_ACCOUNT_CODE, amount, 0, summary),
                (journal_no, offering_code, 0, amount, summary),
            ])
            conn.commit()
            flash(f"수입 전표 {journal_no}가 수정되었습니다.", "success")
            return redirect(url_for("journal"))

        row = conn.execute("""
            SELECT jm.journal_no, jm.journal_date, jm.member_no, jm.offering_code,
                   jm.summary, COALESCE(jd.credit,0) AS amount
            FROM journal_master jm
            JOIN journal_detail jd
              ON jm.journal_no=jd.journal_no
             AND jd.account_code=jm.offering_code
            WHERE jm.journal_no=?
        """, (journal_no,)).fetchone()
        if row is None:
            flash("수입 전표를 찾을 수 없습니다.", "danger")
            return redirect(url_for("journal"))
    except Exception as error:
        conn.rollback()
        flash(str(error), "danger")
        return redirect(url_for("journal"))
    finally:
        conn.close()

    members, income_accounts, _ = load_reference_data()
    return render_template("edit_income.html", row=row, members=members, income_accounts=income_accounts)


@app.route("/journal/expense/<journal_no>/edit", methods=["GET", "POST"])
@login_required
def edit_expense(journal_no):
    conn = get_connection()
    try:
        if request.method == "POST":
            journal_date = request.form.get("journal_date", "").strip()
            expense_code = request.form.get("expense_code", "").strip()
            amount = parse_amount(request.form.get("amount"))
            summary = request.form.get("summary", "").strip() or "현금 지출"

            ensure_cash_account(conn)
            conn.execute("""
                UPDATE journal_master
                SET journal_date=?, member_no=NULL, offering_code=NULL, summary=?, input_user=?
                WHERE journal_no=?
            """, (journal_date, summary, session["user"]["userid"], journal_no))
            conn.execute("DELETE FROM journal_detail WHERE journal_no=?", (journal_no,))
            conn.executemany("""
                INSERT INTO journal_detail
                (journal_no, account_code, debit, credit, remark)
                VALUES (?, ?, ?, ?, ?)
            """, [
                (journal_no, expense_code, amount, 0, summary),
                (journal_no, CASH_ACCOUNT_CODE, 0, amount, summary),
            ])
            conn.commit()
            flash(f"지출 전표 {journal_no}가 수정되었습니다.", "success")
            return redirect(url_for("journal"))

        row = conn.execute("""
            SELECT jm.journal_no, jm.journal_date, jd.account_code AS expense_code,
                   jm.summary, COALESCE(jd.debit,0) AS amount
            FROM journal_master jm
            JOIN journal_detail jd ON jm.journal_no=jd.journal_no
            JOIN accounts a ON jd.account_code=a.code
            WHERE jm.journal_no=? AND a.type='지출' AND COALESCE(jd.debit,0)>0
        """, (journal_no,)).fetchone()
        if row is None:
            flash("지출 전표를 찾을 수 없습니다.", "danger")
            return redirect(url_for("journal"))
    except Exception as error:
        conn.rollback()
        flash(str(error), "danger")
        return redirect(url_for("journal"))
    finally:
        conn.close()

    _, _, expense_accounts = load_reference_data()
    return render_template("edit_expense.html", row=row, expense_accounts=expense_accounts)


@app.post("/journal/<journal_no>/delete")
@login_required
def delete_journal(journal_no):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM journal_detail WHERE journal_no=?", (journal_no,))
        cur = conn.execute("DELETE FROM journal_master WHERE journal_no=?", (journal_no,))
        conn.commit()
        if cur.rowcount:
            flash(f"전표 {journal_no}를 삭제했습니다.", "success")
        else:
            flash("삭제할 전표를 찾지 못했습니다.", "warning")
    except Exception as error:
        conn.rollback()
        flash(str(error), "danger")
    finally:
        conn.close()
    return redirect(url_for("journal"))


@app.route("/daily-statement")
@login_required
def daily_statement():
    target_date = request.args.get("date", date.today().isoformat())
    conn = get_connection()
    try:
        previous_balance = int(round((conn.execute("""
            SELECT COALESCE(SUM(COALESCE(jd.debit,0)-COALESCE(jd.credit,0)),0) AS value
            FROM journal_master jm JOIN journal_detail jd ON jm.journal_no=jd.journal_no
            WHERE jd.account_code=? AND jm.journal_date < ?
        """, (CASH_ACCOUNT_CODE, target_date)).fetchone()["value"] or 0)))

        cash_today = conn.execute("""
            SELECT COALESCE(SUM(jd.debit),0) AS income, COALESCE(SUM(jd.credit),0) AS expense
            FROM journal_master jm JOIN journal_detail jd ON jm.journal_no=jd.journal_no
            WHERE jd.account_code=? AND jm.journal_date=?
        """, (CASH_ACCOUNT_CODE, target_date)).fetchone()
        today_income = int(round(cash_today["income"] or 0))
        today_expense = int(round(cash_today["expense"] or 0))

        incomes = conn.execute("""
            SELECT jm.journal_no,jm.journal_date,COALESCE(m.name,''),COALESCE(a.name,''),
                   COALESCE(jd.credit,0),COALESCE(jm.summary,'')
            FROM journal_master jm
            JOIN journal_detail jd ON jm.journal_no=jd.journal_no AND jd.account_code=jm.offering_code
            LEFT JOIN members m ON jm.member_no=m.member_no
            LEFT JOIN accounts a ON jm.offering_code=a.code
            WHERE jm.journal_date=? AND jm.offering_code IS NOT NULL AND COALESCE(jd.credit,0)>0
            ORDER BY jm.journal_no
        """, (target_date,)).fetchall()

        expenses = conn.execute("""
            SELECT jm.journal_no,jm.journal_date,COALESCE(a.name,''),COALESCE(jd.debit,0),COALESCE(jm.summary,'')
            FROM journal_master jm
            JOIN journal_detail jd ON jm.journal_no=jd.journal_no
            JOIN accounts a ON jd.account_code=a.code
            WHERE jm.journal_date=? AND jm.offering_code IS NULL AND a.type='지출' AND COALESCE(jd.debit,0)>0
            ORDER BY jm.journal_no
        """, (target_date,)).fetchall()
    finally:
        conn.close()

    today_balance = previous_balance + today_income - today_expense
    receipt_total = previous_balance + today_income
    payment_total = today_expense + today_balance
    return render_template(
        "daily_statement.html", target_date=target_date, previous_balance=previous_balance,
        today_income=today_income, today_expense=today_expense, today_balance=today_balance,
        receipt_total=receipt_total, payment_total=payment_total, incomes=incomes, expenses=expenses
    )




@app.route("/cashbook")
@login_required
def cashbook():
    default_start, default_end = default_fiscal_period()
    start_text = request.args.get("start", default_start)
    end_text = request.args.get("end", default_end)

    try:
        if date.fromisoformat(start_text) > date.fromisoformat(end_text):
            raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
        data = fetch_cashbook(start_text, end_text)
    except (ValueError, sqlite3.Error) as error:
        flash(str(error), "danger")
        start_text, end_text = default_start, default_end
        data = fetch_cashbook(start_text, end_text)

    return render_template("cashbook.html", data=data)


@app.route("/ledger")
@login_required
def ledger():
    default_start, default_end = default_fiscal_period()
    start_text = request.args.get("start", default_start)
    end_text = request.args.get("end", default_end)
    account_code = request.args.get("account", "").strip()
    all_mode = request.args.get("all", "") == "1"
    accounts = ledger_accounts()
    selected = None
    ledgers = []

    try:
        if date.fromisoformat(start_text) > date.fromisoformat(end_text):
            raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")

        if all_mode:
            ledgers = fetch_all_ledgers(start_text, end_text)
        elif account_code:
            selected = next((a for a in accounts if str(a["code"]) == account_code), None)
            if selected is None:
                raise ValueError("선택한 계정과목을 찾을 수 없습니다.")
            conn = get_connection()
            try:
                ledgers = [fetch_account_ledger(conn, selected, start_text, end_text)]
            finally:
                conn.close()

    except (ValueError, sqlite3.Error) as error:
        flash(str(error), "danger")

    return render_template(
        "ledger.html",
        accounts=accounts,
        selected=selected,
        ledgers=ledgers,
        start=start_text,
        end=end_text,
        all_mode=all_mode,
    )




@app.route("/trial-balance")
@login_required
def trial_balance():
    default_start, default_end = report_default_fiscal_period()
    start_text = request.args.get("start", default_start)
    end_text = request.args.get("end", default_end)
    include_zero = request.args.get("include_zero", "") == "1"

    try:
        if date.fromisoformat(start_text) > date.fromisoformat(end_text):
            raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
        data = fetch_trial_balance(start_text, end_text, include_zero)
    except ValueError as error:
        flash(str(error), "danger")
        start_text, end_text = default_start, default_end
        data = fetch_trial_balance(start_text, end_text, include_zero)

    return render_template("trial_balance.html", data=data)


@app.route("/income-statement")
@login_required
def income_statement():
    default_start, default_end = report_default_fiscal_period()
    start_text = request.args.get("start", default_start)
    end_text = request.args.get("end", default_end)

    try:
        if date.fromisoformat(start_text) > date.fromisoformat(end_text):
            raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
        data = fetch_income_statement(start_text, end_text)
    except ValueError as error:
        flash(str(error), "danger")
        start_text, end_text = default_start, default_end
        data = fetch_income_statement(start_text, end_text)

    return render_template("income_statement.html", data=data)


@app.route("/balance-sheet")
@login_required
def balance_sheet():
    as_of_text = request.args.get("as_of", date.today().isoformat())
    if request.args.get("year_end") == "1":
        as_of_text = fiscal_year_end()

    try:
        date.fromisoformat(as_of_text)
        data = fetch_balance_sheet(as_of_text)
    except ValueError as error:
        flash(str(error), "danger")
        as_of_text = date.today().isoformat()
        data = fetch_balance_sheet(as_of_text)

    return render_template("balance_sheet.html", data=data)



register_extra_routes(app, login_required)

if __name__ == "__main__":
    create_tables()
    app.run(host="127.0.0.1", port=5000, debug=True)

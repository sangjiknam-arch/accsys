# -*- coding: utf-8 -*-
from datetime import date, datetime
import sqlite3

from flask import flash, redirect, render_template, request, session, url_for

from database import get_connection


ACCOUNT_TYPES = ["", "자산", "부채", "순자산", "수입", "지출"]
POSITIONS = ["", "목사", "전도사", "사모", "장로", "권사", "안수집사", "집사", "권찰", "성도"]


def _login_required():
    if "user" not in session:
        flash("로그인이 필요합니다.", "warning")
        return False
    return True


def _money_to_number(value, allow_zero=False):
    text = str(value or "").replace(",", "").strip()
    if not text:
        if allow_zero:
            return 0
        raise ValueError("금액을 입력하십시오.")
    try:
        number = float(text)
    except ValueError:
        raise ValueError("금액은 숫자로 입력하십시오.")
    if number < 0 or (not allow_zero and number <= 0):
        raise ValueError("금액은 0보다 커야 합니다.")
    return number


def _format_money(value):
    value = float(value or 0)
    return f"{int(value):,}" if value.is_integer() else f"{value:,.2f}"


def _generate_general_journal_no():
    return datetime.now().strftime("GJ%Y%m%d%H%M%S%f")[:20]


# =========================================================
# 계정과목 관리
# =========================================================
def _account_rows():
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT code, name, COALESCE(type, '') AS type
            FROM accounts
            ORDER BY code
        """).fetchall()
    finally:
        conn.close()


def register_extra_routes(app, login_required):

    @app.route("/accounts", methods=["GET", "POST"])
    @login_required
    def accounts_manage():
        if request.method == "POST":
            action = request.form.get("action", "")
            code = request.form.get("code", "").strip()
            name = request.form.get("name", "").strip()
            account_type = request.form.get("type", "").strip()

            if action in ("save", "update") and (not code or not name):
                flash("계정코드와 계정명을 입력하십시오.", "danger")
                return redirect(url_for("accounts_manage"))

            if account_type not in ACCOUNT_TYPES:
                flash("계정구분이 올바르지 않습니다.", "danger")
                return redirect(url_for("accounts_manage"))

            conn = get_connection()
            try:
                if action == "save":
                    conn.execute(
                        "INSERT INTO accounts (code, name, type) VALUES (?, ?, ?)",
                        (code, name, account_type),
                    )
                    conn.commit()
                    flash("계정과목을 저장했습니다.", "success")

                elif action == "update":
                    original_code = request.form.get("original_code", "").strip()
                    if not original_code:
                        raise ValueError("수정할 계정과목을 선택하십시오.")
                    conn.execute(
                        "UPDATE accounts SET name = ?, type = ? WHERE code = ?",
                        (name, account_type, original_code),
                    )
                    conn.commit()
                    flash("계정과목을 수정했습니다.", "success")

                elif action == "delete":
                    target = request.form.get("original_code", "").strip() or code
                    used = conn.execute(
                        "SELECT COUNT(*) AS c FROM journal_detail WHERE account_code = ?",
                        (target,),
                    ).fetchone()["c"]
                    budget_used = conn.execute("""
                        SELECT COUNT(*) AS c
                        FROM budget b
                        JOIN accounts a ON b.account_id = a.id
                        WHERE a.code = ?
                    """, (target,)).fetchone()["c"]

                    if used or budget_used:
                        flash("분개자료 또는 예산에서 사용 중인 계정은 삭제할 수 없습니다.", "danger")
                    else:
                        conn.execute("DELETE FROM accounts WHERE code = ?", (target,))
                        conn.commit()
                        flash("계정과목을 삭제했습니다.", "success")

            except sqlite3.IntegrityError:
                conn.rollback()
                flash("이미 존재하는 계정코드이거나 연결된 자료가 있습니다.", "danger")
            except (sqlite3.Error, ValueError) as error:
                conn.rollback()
                flash(str(error), "danger")
            finally:
                conn.close()

            return redirect(url_for("accounts_manage"))

        selected_code = request.args.get("edit", "").strip()
        selected = None
        if selected_code:
            conn = get_connection()
            try:
                selected = conn.execute(
                    "SELECT code, name, COALESCE(type, '') AS type FROM accounts WHERE code = ?",
                    (selected_code,),
                ).fetchone()
            finally:
                conn.close()

        return render_template(
            "accounts.html",
            rows=_account_rows(),
            selected=selected,
            account_types=ACCOUNT_TYPES,
        )

    # =========================================================
    # 교인 관리
    # =========================================================
    @app.route("/members", methods=["GET", "POST"])
    @login_required
    def members_manage():
        if request.method == "POST":
            action = request.form.get("action", "")
            member_no = request.form.get("member_no", "").strip()
            name = request.form.get("name", "").strip()
            phone = request.form.get("phone", "").strip()
            address = request.form.get("address", "").strip()
            reg_date = request.form.get("reg_date", "").strip()
            baptism_date = request.form.get("baptism_date", "").strip()
            position = request.form.get("position", "").strip()
            memo = request.form.get("memo", "").strip()

            if action in ("save", "update") and not name:
                flash("교인 성명을 입력하십시오.", "danger")
                return redirect(url_for("members_manage"))

            conn = get_connection()
            try:
                if action == "save":
                    cursor = conn.execute("""
                        INSERT INTO members
                            (name, phone, address, reg_date, baptism_date, position, memo)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (name, phone, address, reg_date, baptism_date, position, memo))
                    # 새 교인의 member_no도 반드시 지정하여 이후 수정·삭제와 헌금자
                    # 선택 화면에서 정상 조회되도록 합니다.
                    new_id = cursor.lastrowid
                    taken = conn.execute(
                        "SELECT 1 FROM members WHERE member_no = ? AND rowid <> ?",
                        (new_id, new_id),
                    ).fetchone()
                    if taken:
                        next_no = conn.execute(
                            "SELECT COALESCE(MAX(member_no), 0) + 1 FROM members"
                        ).fetchone()[0]
                    else:
                        next_no = new_id
                    conn.execute(
                        "UPDATE members SET member_no = ? WHERE rowid = ?",
                        (next_no, new_id),
                    )
                    conn.commit()
                    flash("교인정보를 저장했습니다.", "success")

                elif action == "update":
                    if not member_no:
                        raise ValueError("수정할 교인을 선택하십시오.")
                    conn.execute("""
                        UPDATE members
                        SET name=?, phone=?, address=?, reg_date=?,
                            baptism_date=?, position=?, memo=?
                        WHERE member_no=?
                    """, (name, phone, address, reg_date, baptism_date,
                          position, memo, int(member_no)))
                    conn.commit()
                    flash("교인정보를 수정했습니다.", "success")

                elif action == "delete":
                    if not member_no:
                        raise ValueError("삭제할 교인을 선택하십시오.")
                    used = conn.execute(
                        "SELECT COUNT(*) AS c FROM journal_master WHERE member_no = ?",
                        (int(member_no),),
                    ).fetchone()["c"]
                    if used:
                        flash("헌금 또는 분개자료에서 사용 중인 교인은 삭제할 수 없습니다.", "danger")
                    else:
                        conn.execute("DELETE FROM members WHERE member_no = ?", (int(member_no),))
                        conn.commit()
                        flash("교인정보를 삭제했습니다.", "success")

            except (sqlite3.Error, ValueError) as error:
                conn.rollback()
                flash(str(error), "danger")
            finally:
                conn.close()

            return redirect(url_for("members_manage"))

        keyword = request.args.get("q", "").strip()
        selected_no = request.args.get("edit", "").strip()
        conn = get_connection()
        try:
            if keyword:
                w = f"%{keyword}%"
                rows = conn.execute("""
                    SELECT member_no, name, COALESCE(phone,''), COALESCE(address,''),
                           COALESCE(reg_date,''), COALESCE(baptism_date,''),
                           COALESCE(position,''), COALESCE(memo,'')
                    FROM members
                    WHERE name LIKE ? OR phone LIKE ?
                    ORDER BY name, member_no
                """, (w, w)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT member_no, name, COALESCE(phone,''), COALESCE(address,''),
                           COALESCE(reg_date,''), COALESCE(baptism_date,''),
                           COALESCE(position,''), COALESCE(memo,'')
                    FROM members
                    ORDER BY name, member_no
                """).fetchall()

            selected = None
            if selected_no:
                selected = conn.execute("""
                    SELECT member_no, name, COALESCE(phone,''), COALESCE(address,''),
                           COALESCE(reg_date,''), COALESCE(baptism_date,''),
                           COALESCE(position,''), COALESCE(memo,'')
                    FROM members WHERE member_no=?
                """, (int(selected_no),)).fetchone()
        finally:
            conn.close()

        return render_template(
            "members.html",
            rows=rows,
            selected=selected,
            positions=POSITIONS,
            keyword=keyword,
        )

    # =========================================================
    # 일반분개
    # =========================================================
    @app.route("/general-journal", methods=["GET", "POST"])
    @login_required
    def general_journal():
        conn = get_connection()
        try:
            accounts = conn.execute("""
                SELECT code, name, COALESCE(type,'') AS type
                FROM accounts ORDER BY code
            """).fetchall()
        finally:
            conn.close()

        if request.method == "POST":
            action = request.form.get("action", "save")
            journal_no = request.form.get("journal_no", "").strip()
            journal_date = request.form.get("journal_date", "").strip()
            summary = request.form.get("summary", "").strip()

            if action == "delete":
                if not journal_no.startswith("GJ"):
                    flash("일반분개(GJ)만 이 화면에서 삭제할 수 있습니다.", "danger")
                    return redirect(url_for("general_journal"))
                conn = get_connection()
                try:
                    conn.execute("DELETE FROM journal_detail WHERE journal_no=?", (journal_no,))
                    conn.execute("DELETE FROM journal_master WHERE journal_no=?", (journal_no,))
                    conn.commit()
                    flash("일반분개를 삭제했습니다.", "success")
                except sqlite3.Error as error:
                    conn.rollback()
                    flash(str(error), "danger")
                finally:
                    conn.close()
                return redirect(url_for("general_journal"))

            account_codes = request.form.getlist("account_code")
            debits = request.form.getlist("debit")
            credits = request.form.getlist("credit")
            remarks = request.form.getlist("remark")

            try:
                if not journal_date:
                    raise ValueError("분개일자를 입력하십시오.")
                if not summary:
                    raise ValueError("전체 적요를 입력하십시오.")

                details = []
                max_len = max(len(account_codes), len(debits), len(credits), len(remarks), 0)
                for i in range(max_len):
                    code = account_codes[i].strip() if i < len(account_codes) else ""
                    debit_text = debits[i] if i < len(debits) else ""
                    credit_text = credits[i] if i < len(credits) else ""
                    remark = remarks[i].strip() if i < len(remarks) else ""
                    debit = _money_to_number(debit_text, allow_zero=True)
                    credit = _money_to_number(credit_text, allow_zero=True)

                    if not code and debit == 0 and credit == 0 and not remark:
                        continue
                    if not code:
                        raise ValueError(f"{i+1}행의 계정과목을 선택하십시오.")
                    if debit > 0 and credit > 0:
                        raise ValueError(f"{i+1}행에는 차변과 대변을 동시에 입력할 수 없습니다.")
                    if debit == 0 and credit == 0:
                        raise ValueError(f"{i+1}행의 차변 또는 대변 금액을 입력하십시오.")
                    details.append((code, debit, credit, remark))

                if len(details) < 2:
                    raise ValueError("일반분개는 최소 두 개의 상세 행이 필요합니다.")

                total_debit = sum(d[1] for d in details)
                total_credit = sum(d[2] for d in details)
                if round(total_debit, 2) != round(total_credit, 2):
                    raise ValueError(
                        f"차변 합계와 대변 합계가 일치하지 않습니다. "
                        f"(차변 {_format_money(total_debit)} / 대변 {_format_money(total_credit)})"
                    )
                if total_debit <= 0:
                    raise ValueError("분개 금액은 0보다 커야 합니다.")

                current_user = session.get("user", {}).get("userid", "admin")
                is_update = bool(journal_no)
                if not journal_no:
                    journal_no = _generate_general_journal_no()
                elif not journal_no.startswith("GJ"):
                    raise ValueError("일반분개 번호가 아닙니다.")

                conn = get_connection()
                try:
                    conn.execute("BEGIN")
                    if is_update:
                        conn.execute("""
                            UPDATE journal_master
                            SET journal_date=?, member_no=NULL, offering_code=NULL,
                                summary=?, input_user=?
                            WHERE journal_no=?
                        """, (journal_date, summary, current_user, journal_no))
                        conn.execute("DELETE FROM journal_detail WHERE journal_no=?", (journal_no,))
                    else:
                        conn.execute("""
                            INSERT INTO journal_master
                                (journal_no, journal_date, member_no, offering_code, summary, input_user)
                            VALUES (?, ?, NULL, NULL, ?, ?)
                        """, (journal_no, journal_date, summary, current_user))

                    conn.executemany("""
                        INSERT INTO journal_detail
                            (journal_no, account_code, debit, credit, remark)
                        VALUES (?, ?, ?, ?, ?)
                    """, [(journal_no, *d) for d in details])
                    conn.commit()
                finally:
                    conn.close()

                flash("일반분개를 수정했습니다." if is_update else "일반분개를 저장했습니다.", "success")
                return redirect(url_for("general_journal"))

            except (ValueError, sqlite3.Error) as error:
                flash(str(error), "danger")

        edit_no = request.args.get("edit", "").strip()
        edit_master = None
        edit_details = []
        conn = get_connection()
        try:
            if edit_no:
                edit_master = conn.execute("""
                    SELECT journal_no, journal_date, COALESCE(summary,'')
                    FROM journal_master
                    WHERE journal_no=? AND journal_no LIKE 'GJ%'
                """, (edit_no,)).fetchone()
                if edit_master:
                    edit_details = conn.execute("""
                        SELECT account_code, COALESCE(debit,0) AS debit,
                               COALESCE(credit,0) AS credit, COALESCE(remark,'') AS remark
                        FROM journal_detail
                        WHERE journal_no=?
                        ORDER BY id
                    """, (edit_no,)).fetchall()

            recent = conn.execute("""
                SELECT jm.journal_no, jm.journal_date, COALESCE(jm.summary,'') AS summary,
                       COALESCE(SUM(jd.debit),0) AS debit_total,
                       COALESCE(SUM(jd.credit),0) AS credit_total
                FROM journal_master jm
                JOIN journal_detail jd ON jm.journal_no=jd.journal_no
                WHERE jm.journal_no LIKE 'GJ%'
                GROUP BY jm.journal_no, jm.journal_date, jm.summary
                ORDER BY jm.journal_date DESC, jm.journal_no DESC
                LIMIT 200
            """).fetchall()
        finally:
            conn.close()

        return render_template(
            "general_journal.html",
            accounts=accounts,
            recent=recent,
            edit_master=edit_master,
            edit_details=edit_details,
            today=date.today().isoformat(),
        )

    # =========================================================
    # 예산관리
    # =========================================================
    def normalized_budget_accounts(conn):
        return conn.execute("""
            SELECT id, TRIM(code) AS code, TRIM(name) AS name,
                   CASE
                     WHEN REPLACE(TRIM(type),' ','') IN ('수입','수익','수입계정','수익계정') THEN '수입'
                     WHEN REPLACE(TRIM(type),' ','') IN ('지출','비용','지출계정','비용계정') THEN '지출'
                   END AS normalized_type
            FROM accounts
            WHERE REPLACE(TRIM(type),' ','') IN (
                '수입','수익','수입계정','수익계정',
                '지출','비용','지출계정','비용계정'
            )
              AND TRIM(name) NOT IN ('새합천교회지원','가조반석교회지원','대출이자','원금상환')
              AND TRIM(name) NOT LIKE '%선교%'
              AND (TRIM(name) NOT LIKE '%건축%' OR TRIM(name)='건축재정대체')
            ORDER BY CASE
                WHEN REPLACE(TRIM(type),' ','') IN ('수입','수익','수입계정','수익계정') THEN 1
                ELSE 2 END, TRIM(code)
        """).fetchall()

    def account_actual(conn, account_code, account_type, actual_year):
        row = conn.execute("""
            SELECT COALESCE(SUM(jd.debit),0) AS debit,
                   COALESCE(SUM(jd.credit),0) AS credit
            FROM journal_detail jd
            JOIN journal_master jm ON jd.journal_no=jm.journal_no
            WHERE jd.account_code=? AND strftime('%Y', jm.journal_date)=?
        """, (account_code, str(actual_year))).fetchone()
        debit = float(row["debit"] or 0)
        credit = float(row["credit"] or 0)
        if account_type == "수입":
            return int(round(credit-debit))
        if account_type == "지출":
            return int(round(debit-credit))
        return int(round(abs(debit-credit)))

    @app.route("/budget", methods=["GET", "POST"])
    @login_required
    def budget_manage():
        actual_year = date.today().year
        budget_year = int(request.args.get("year") or request.form.get("year") or (actual_year + 1))

        if request.method == "POST":
            action = request.form.get("action", "")
            budget_id = request.form.get("budget_id", "").strip()
            account_id = request.form.get("account_id", "").strip()
            amount_text = request.form.get("amount", "").strip()
            try:
                if action in ("save", "update"):
                    if not account_id:
                        raise ValueError("계정과목을 선택하십시오.")
                    amount = int(_money_to_number(amount_text, allow_zero=True))
                    conn = get_connection()
                    try:
                        if action == "save":
                            exists = conn.execute(
                                "SELECT id FROM budget WHERE year=? AND account_id=?",
                                (budget_year, int(account_id)),
                            ).fetchone()
                            if exists:
                                raise ValueError("해당 연도와 계정과목의 예산이 이미 등록되어 있습니다.")
                            conn.execute(
                                "INSERT INTO budget(year,account_id,amount) VALUES(?,?,?)",
                                (budget_year, int(account_id), amount),
                            )
                            flash("예산을 저장했습니다.", "success")
                        else:
                            if not budget_id:
                                raise ValueError("수정할 예산을 선택하십시오.")
                            exists = conn.execute("""
                                SELECT id FROM budget
                                WHERE year=? AND account_id=? AND id<>?
                            """, (budget_year, int(account_id), int(budget_id))).fetchone()
                            if exists:
                                raise ValueError("해당 연도와 계정과목의 예산이 이미 등록되어 있습니다.")
                            conn.execute("""
                                UPDATE budget SET year=?, account_id=?, amount=? WHERE id=?
                            """, (budget_year, int(account_id), amount, int(budget_id)))
                            flash("예산을 수정했습니다.", "success")
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                    finally:
                        conn.close()

                elif action == "delete":
                    if not budget_id:
                        raise ValueError("삭제할 예산을 선택하십시오.")
                    conn = get_connection()
                    try:
                        conn.execute("DELETE FROM budget WHERE id=?", (int(budget_id),))
                        conn.commit()
                    finally:
                        conn.close()
                    flash("예산을 삭제했습니다.", "success")

            except (ValueError, sqlite3.Error) as error:
                flash(str(error), "danger")

            return redirect(url_for("budget_manage", year=budget_year))

        conn = get_connection()
        try:
            accounts = normalized_budget_accounts(conn)
            rows = []
            income_actual = expense_actual = income_budget = expense_budget = 0
            for a in accounts:
                b = conn.execute(
                    "SELECT id, amount FROM budget WHERE year=? AND account_id=?",
                    (budget_year, a["id"]),
                ).fetchone()
                amount = int(b["amount"] or 0) if b else 0
                actual = account_actual(conn, a["code"], a["normalized_type"], actual_year)
                diff = amount - actual
                rate = "신규" if actual == 0 and amount != 0 else (
                    "0.0%" if actual == 0 else f"{diff/abs(actual)*100:+.1f}%"
                )
                rows.append({
                    "budget_id": b["id"] if b else None,
                    "account_id": a["id"], "code": a["code"], "name": a["name"],
                    "type": a["normalized_type"], "actual": actual, "budget": amount,
                    "difference": diff, "rate": rate,
                })
                if a["normalized_type"] == "수입":
                    income_actual += actual
                    income_budget += amount
                else:
                    expense_actual += actual
                    expense_budget += amount

            edit_id = request.args.get("edit", "").strip()
            selected = next((r for r in rows if edit_id and str(r["budget_id"]) == edit_id), None)
        finally:
            conn.close()

        return render_template(
            "budget.html", rows=rows, accounts=accounts, selected=selected,
            actual_year=actual_year, budget_year=budget_year,
            income_actual=income_actual, expense_actual=expense_actual,
            income_budget=income_budget, expense_budget=expense_budget,
            budget_balance=income_budget-expense_budget,
        )

    # =========================================================
    # 기부금 영수증
    # =========================================================
    def donation_summary(conn, donor_name, year):
        result = {"십일조":0, "감사헌금":0, "선교헌금":0, "건축헌금":0, "기타헌금":0}
        if not donor_name:
            return result
        rows = conn.execute("""
            SELECT CASE
                WHEN a.name LIKE '%십일조%' THEN '십일조'
                WHEN a.name LIKE '%감사%' THEN '감사헌금'
                WHEN a.name LIKE '%선교%' THEN '선교헌금'
                WHEN a.name LIKE '%건축%' THEN '건축헌금'
                ELSE '기타헌금' END AS donation_group,
                COALESCE(SUM(jd.credit),0) AS total_amount
            FROM journal_master jm
            JOIN members m ON jm.member_no=m.member_no
            JOIN journal_detail jd
              ON jm.journal_no=jd.journal_no AND jd.account_code=jm.offering_code
            JOIN accounts a ON jm.offering_code=a.code
            WHERE m.name=? AND a.type='수입'
              AND strftime('%Y', jm.journal_date)=?
              AND COALESCE(jd.credit,0)>0
            GROUP BY donation_group
        """, (donor_name, str(year))).fetchall()
        for row in rows:
            result[row["donation_group"]] = int(row["total_amount"] or 0)
        return result

    @app.route("/donation-receipt", methods=["GET", "POST"])
    @login_required
    def donation_receipt():
        conn = get_connection()
        try:
            church = conn.execute("""
                SELECT COALESCE(church_name,''), COALESCE(unique_number,''),
                       COALESCE(address,''), COALESCE(representative,''),
                       COALESCE(legal_basis,'')
                FROM church_info WHERE id=1
            """).fetchone()
            members = conn.execute("SELECT name, COALESCE(address,'') AS address FROM members ORDER BY name").fetchall()
        finally:
            conn.close()

        today = date.today()
        year = int(request.values.get("year") or (today.year - 1))
        donor_name = request.values.get("donor_name", "").strip()
        donor_number = request.values.get("donor_number", "").strip()
        donor_address = request.values.get("donor_address", "").strip()
        serial_number = request.values.get("serial_number", f"{today.year}-{today.strftime('%m%d')}-001").strip()
        issue_date = request.values.get("issue_date", today.isoformat()).strip()
        code = request.values.get("donation_code", "41").strip()

        if request.method == "POST" and request.form.get("action") == "save_church":
            values = (
                request.form.get("church_name","").strip(),
                request.form.get("unique_number","").strip(),
                request.form.get("church_address","").strip(),
                request.form.get("representative","").strip(),
                request.form.get("legal_basis","").strip(),
            )
            conn = get_connection()
            try:
                conn.execute("""
                    INSERT INTO church_info(id,church_name,unique_number,address,representative,legal_basis)
                    VALUES(1,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        church_name=excluded.church_name,
                        unique_number=excluded.unique_number,
                        address=excluded.address,
                        representative=excluded.representative,
                        legal_basis=excluded.legal_basis
                """, values)
                conn.commit()
                flash("교회정보를 저장했습니다.", "success")
            finally:
                conn.close()
            return redirect(url_for("donation_receipt"))

        conn = get_connection()
        try:
            summary = donation_summary(conn, donor_name, year)
        finally:
            conn.close()

        total = sum(summary.values())
        preview = request.values.get("preview") == "1"

        return render_template(
            "donation_receipt.html",
            church=church, members=members, year=year, donor_name=donor_name,
            donor_number=donor_number, donor_address=donor_address,
            serial_number=serial_number, issue_date=issue_date, donation_code=code,
            summary=summary, total=total, preview=preview,
        )

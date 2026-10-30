# -*- coding: utf-8 -*-

import hashlib
import hmac
import os
import sqlite3


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.environ.get("CHURCH_DB_PATH") or os.path.join(BASE_DIR, "church.db")


MISSION_INCOME_CATEGORIES = [
    "개인헌금",
    "수입이자",
    "기타",
]

MISSION_EXPENSE_CATEGORIES = [
    "필리핀선교",
    "라오스선교",
    "일본선교",
    "새합천교회지원",
    "가조반석교회지원",
    "한국컴패션",
    "기타선교지원",
]

BUILDING_INCOME_CATEGORIES = [
    "개인헌금",
    "수입이자",
    "일반재정대체",
    "기타수입",
]

BUILDING_EXPENSE_CATEGORIES = [
    "원금상환",
    "대출이자",
    "화재보험료",
    "기타지출",
]


PASSWORD_PREFIX = "scrypt"
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64


def hash_password(password):
    """비밀번호를 scrypt 해시 문자열로 변환합니다."""
    if not isinstance(password, str) or not password:
        raise ValueError("비밀번호가 비어 있습니다.")

    salt = os.urandom(16)
    password_hash = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )

    return (
        f"{PASSWORD_PREFIX}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$"
        f"{salt.hex()}${password_hash.hex()}"
    )


def is_hashed_password(stored_password):
    """저장된 값이 현재 scrypt 해시 형식인지 확인합니다."""
    return str(stored_password or "").startswith(
        f"{PASSWORD_PREFIX}$"
    )


def verify_password(password, stored_password):
    """
    입력 비밀번호와 저장 비밀번호를 비교합니다.

    기존 데이터베이스에 평문 비밀번호가 남아 있어도
    로그인할 수 있도록 한시적으로 평문 비교를 지원합니다.
    """
    stored_password = str(stored_password or "")

    if not is_hashed_password(stored_password):
        return hmac.compare_digest(
            str(password),
            stored_password,
        )

    try:
        (
            prefix,
            n_text,
            r_text,
            p_text,
            salt_hex,
            expected_hex,
        ) = stored_password.split("$", 5)

        if prefix != PASSWORD_PREFIX:
            return False

        salt = bytes.fromhex(salt_hex)
        expected_hash = bytes.fromhex(expected_hex)

        actual_hash = hashlib.scrypt(
            str(password).encode("utf-8"),
            salt=salt,
            n=int(n_text),
            r=int(r_text),
            p=int(p_text),
            dklen=len(expected_hash),
        )

        return hmac.compare_digest(
            actual_hash,
            expected_hash,
        )

    except (ValueError, TypeError):
        return False


def get_connection():
    """church.db 데이터베이스 연결."""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_table_columns(conn, table_name):
    """지정한 테이블의 실제 열 이름 목록을 반환합니다."""
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    return [row["name"] for row in cur.fetchall()]


def get_users_columns(conn):
    """users 테이블의 실제 열 이름을 반환합니다."""
    return get_table_columns(conn, "users")


def ensure_users_schema(conn):
    """기존 users 테이블에 로그인 권한 열이 없으면 보완합니다."""
    columns = get_users_columns(conn)

    if not columns:
        return

    if "role" not in columns and "level" not in columns:
        conn.execute("""
            ALTER TABLE users
            ADD COLUMN role TEXT NOT NULL DEFAULT 'user'
        """)
        # A legacy admin account must remain an administrator after migration.
        # Change only when adding a previously missing permission column.
        conn.execute("""
            UPDATE users SET role = 'admin' WHERE userid = 'admin'
        """)


def get_permission_column(conn):
    """users 테이블에서 사용하는 권한 열 이름을 반환합니다."""
    columns = get_users_columns(conn)

    if "role" in columns:
        return "role"

    if "level" in columns:
        return "level"

    return None


def ensure_members_schema(conn):
    """
    기존 members 테이블 구조를 확인하여 member_no 열을 보완합니다.

    지원하는 기존 구조:
    1. id 기본키가 있는 구조
    2. member_no 기본키만 있는 구조
    3. id와 member_no가 모두 없는 오래된 구조

    id 열이 없을 때는 SQLite의 rowid를 이용해 비어 있는
    member_no를 안전하게 순번으로 채웁니다.
    """
    columns = get_table_columns(conn, "members")

    if not columns:
        return

    cur = conn.cursor()

    # 교인 관리 화면이 사용하는 등록일/세례일 필드가 구형 DB에 없으면 추가합니다.
    # 기존 데이터나 기존 baptism 열을 삭제하거나 덮어쓰지 않습니다.
    for column_name in ("reg_date", "baptism_date"):
        if column_name not in columns:
            cur.execute(f"ALTER TABLE members ADD COLUMN {column_name} TEXT")
            columns.append(column_name)

    # member_no 열이 없는 기존 DB에는 열을 추가합니다.
    if "member_no" not in columns:
        cur.execute(
            "ALTER TABLE members ADD COLUMN member_no INTEGER"
        )
        columns.append("member_no")

    # member_no가 비어 있는 행만 보완합니다.
    cur.execute("""
        SELECT COUNT(*) AS count_value
        FROM members
        WHERE member_no IS NULL
    """)
    null_count = int(cur.fetchone()["count_value"] or 0)

    if null_count > 0:
        if "id" in columns:
            # id 열이 있는 구조는 기존 id를 교인번호로 사용합니다.
            cur.execute("""
                UPDATE members
                SET member_no = id
                WHERE member_no IS NULL
            """)
        else:
            # id 열이 없는 구조는 rowid 순서대로 새 번호를 부여합니다.
            cur.execute("""
                SELECT COALESCE(MAX(member_no), 0) AS max_member_no
                FROM members
            """)
            next_member_no = int(
                cur.fetchone()["max_member_no"] or 0
            ) + 1

            cur.execute("""
                SELECT rowid
                FROM members
                WHERE member_no IS NULL
                ORDER BY rowid
            """)

            for row in cur.fetchall():
                cur.execute("""
                    UPDATE members
                    SET member_no = ?
                    WHERE rowid = ?
                """, (
                    next_member_no,
                    row["rowid"],
                ))
                next_member_no += 1

    # 선교·건축 회계에서 member_no를 안정적으로 참조하도록 합니다.
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_members_member_no
        ON members(member_no)
    """)


def create_special_account_tables(conn):
    """선교 회계와 건축 회계용 독립 테이블 및 초기 항목을 생성합니다."""
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS special_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fund_type TEXT NOT NULL
                CHECK (fund_type IN ('MISSION', 'BUILDING')),
            transaction_type TEXT NOT NULL
                CHECK (transaction_type IN ('INCOME', 'EXPENSE')),
            category_name TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1
                CHECK (active IN (0, 1)),
            UNIQUE (
                fund_type,
                transaction_type,
                category_name
            )
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS special_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fund_type TEXT NOT NULL
                CHECK (fund_type IN ('MISSION', 'BUILDING')),
            transaction_type TEXT NOT NULL
                CHECK (transaction_type IN ('INCOME', 'EXPENSE')),
            transaction_date TEXT NOT NULL,
            member_no INTEGER,
            institution_name TEXT,
            category_name TEXT NOT NULL,
            description TEXT,
            amount INTEGER NOT NULL DEFAULT 0
                CHECK (amount >= 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT,
            FOREIGN KEY (member_no)
                REFERENCES members(member_no)
                ON UPDATE CASCADE
                ON DELETE SET NULL
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_special_transactions_fund_date
        ON special_transactions(fund_type, transaction_date)
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_special_transactions_member
        ON special_transactions(member_no)
    """)

    seed_sets = [
        ("MISSION", "INCOME", MISSION_INCOME_CATEGORIES),
        ("MISSION", "EXPENSE", MISSION_EXPENSE_CATEGORIES),
        ("BUILDING", "INCOME", BUILDING_INCOME_CATEGORIES),
        ("BUILDING", "EXPENSE", BUILDING_EXPENSE_CATEGORIES),
    ]

    for fund_type, transaction_type, categories in seed_sets:
        for sort_order, category_name in enumerate(categories, start=1):
            cur.execute("""
                INSERT OR IGNORE INTO special_categories (
                    fund_type,
                    transaction_type,
                    category_name,
                    sort_order,
                    active
                )
                VALUES (?, ?, ?, ?, 1)
            """, (
                fund_type,
                transaction_type,
                category_name,
                sort_order,
            ))


def create_tables():
    """프로그램에서 사용하는 모든 테이블을 생성합니다."""
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                type TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT,
                address TEXT,
                memo TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS budget (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                jdate TEXT NOT NULL,
                account_id INTEGER NOT NULL,
                customer_id INTEGER,
                debit INTEGER DEFAULT 0,
                credit INTEGER DEFAULT 0,
                remark TEXT,
                FOREIGN KEY(account_id) REFERENCES accounts(id),
                FOREIGN KEY(customer_id) REFERENCES customers(id)
            )
        """)

        # 복식부기 전표 원장: 기존 DB의 전표는 보존하고 누락된 테이블만 생성
        cur.execute("""
            CREATE TABLE IF NOT EXISTS journal_master (
                journal_no TEXT PRIMARY KEY,
                journal_date TEXT NOT NULL,
                member_no INTEGER,
                offering_code TEXT,
                summary TEXT,
                input_user TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS journal_detail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                journal_no TEXT NOT NULL,
                account_code TEXT NOT NULL,
                debit INTEGER NOT NULL DEFAULT 0,
                credit INTEGER NOT NULL DEFAULT 0,
                remark TEXT,
                FOREIGN KEY (journal_no) REFERENCES journal_master(journal_no)
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_journal_master_date
            ON journal_master(journal_date)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_journal_detail_account
            ON journal_detail(account_code)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS offering (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                odate TEXT NOT NULL,
                member TEXT,
                offering_type TEXT,
                amount INTEGER NOT NULL,
                memo TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_no INTEGER UNIQUE,
                name TEXT NOT NULL,
                gender TEXT,
                birth TEXT,
                phone TEXT,
                address TEXT,
                department TEXT,
                position TEXT,
                baptism TEXT,
                memo TEXT
            )
        """)

        ensure_members_schema(conn)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                userid TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user'
            )
        """)

        ensure_users_schema(conn)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS church_info (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                church_name TEXT,
                unique_number TEXT,
                address TEXT,
                representative TEXT,
                legal_basis TEXT
            )
        """)

        cur.execute("""
            INSERT OR IGNORE INTO church_info (
                id,
                church_name,
                unique_number,
                address,
                representative,
                legal_basis
            )
            VALUES (
                1,
                '',
                '',
                '',
                '',
                '법인세법 시행령 제39조제1항제1호마목'
            )
        """)

        # 일반 회계와 분리된 선교·건축 회계 테이블 생성
        create_special_account_tables(conn)

        # 기본 관리자 계정 생성
        create_default_admin(conn)

        conn.commit()

    except sqlite3.Error:
        conn.rollback()
        raise

    finally:
        conn.close()


def create_default_admin(conn=None):
    """
    기본 관리자 계정을 생성합니다.

    아이디: admin
    초기 비밀번호: 12345678

    새로 생성하는 계정의 비밀번호는 scrypt 해시로 저장합니다.
    이미 admin 계정이 있으면 기존 비밀번호는 변경하지 않습니다.
    """
    own_connection = conn is None

    if own_connection:
        conn = get_connection()

    cur = conn.cursor()

    try:
        ensure_users_schema(conn)
        permission_column = get_permission_column(conn)

        if permission_column is None:
            raise sqlite3.OperationalError(
                "users 테이블에 role 또는 level 열이 없습니다."
            )

        cur.execute(
            "SELECT id FROM users WHERE userid = ?",
            ("admin",),
        )

        admin_row = cur.fetchone()
        admin_permission = (
            "admin" if permission_column == "role" else "ADMIN"
        )

        if admin_row is None:
            sql = f"""
                INSERT INTO users (
                    userid,
                    password,
                    name,
                    {permission_column}
                )
                VALUES (?, ?, ?, ?)
            """

            cur.execute(sql, (
                "admin",
                hash_password("12345678"),
                "관리자",
                admin_permission,
            ))

        if own_connection:
            conn.commit()

    except sqlite3.Error:
        if own_connection:
            conn.rollback()
        raise

    finally:
        if own_connection:
            conn.close()


def change_user_password(
    userid,
    current_password,
    new_password,
):
    """
    현재 비밀번호를 확인한 후 새 비밀번호로 변경합니다.

    반환값:
        (True, 성공 메시지)
        (False, 실패 메시지)
    """
    userid = str(userid or "").strip()

    if not userid:
        return False, "사용자 아이디가 없습니다."

    if not current_password:
        return False, "현재 비밀번호를 입력하십시오."

    if not new_password:
        return False, "새 비밀번호를 입력하십시오."

    if not (len(new_password) == 8 and new_password.isdigit()):
        return False, "새 비밀번호는 숫자 정확히 8자리여야 합니다."

    if current_password == new_password:
        return False, "새 비밀번호는 현재 비밀번호와 달라야 합니다."

    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT id, password
            FROM users
            WHERE userid = ?
        """, (userid,))

        row = cur.fetchone()

        if row is None:
            return False, "사용자 계정을 찾을 수 없습니다."

        if not verify_password(
            current_password,
            row["password"],
        ):
            return False, "현재 비밀번호가 올바르지 않습니다."

        cur.execute("""
            UPDATE users
            SET password = ?
            WHERE userid = ?
        """, (
            hash_password(new_password),
            userid,
        ))

        conn.commit()
        return True, "비밀번호를 안전하게 변경했습니다."

    except sqlite3.Error as error:
        conn.rollback()
        return (
            False,
            "비밀번호 변경 중 오류가 발생했습니다.\n\n"
            f"{error}",
        )

    finally:
        conn.close()



def reset_admin_password(new_password="12345678"):
    """
    관리자 계정의 비밀번호를 지정한 값으로 초기화합니다.

    이 함수는 main.py의 create_tables()에서는 자동 실행되지 않습니다.
    database.py를 직접 실행할 때만 호출하도록 구성합니다.
    """
    if not new_password:
        raise ValueError("초기화할 비밀번호가 비어 있습니다.")

    if not (len(str(new_password)) == 8 and str(new_password).isdigit()):
        raise ValueError("관리자 비밀번호는 숫자 정확히 8자리여야 합니다.")

    conn = get_connection()
    cur = conn.cursor()

    try:
        ensure_users_schema(conn)
        permission_column = get_permission_column(conn)

        if permission_column is None:
            raise sqlite3.OperationalError(
                "users 테이블에 role 또는 level 열이 없습니다."
            )

        permission_value = (
            "admin" if permission_column == "role" else "ADMIN"
        )

        cur.execute("""
            SELECT id
            FROM users
            WHERE userid = ?
        """, ("admin",))

        row = cur.fetchone()
        password_hash = hash_password(new_password)

        if row is None:
            sql = f"""
                INSERT INTO users (
                    userid,
                    password,
                    name,
                    {permission_column}
                )
                VALUES (?, ?, ?, ?)
            """
            cur.execute(sql, (
                "admin",
                password_hash,
                "관리자",
                permission_value,
            ))
        else:
            sql = f"""
                UPDATE users
                SET
                    password = ?,
                    name = ?,
                    {permission_column} = ?
                WHERE userid = ?
            """
            cur.execute(sql, (
                password_hash,
                "관리자",
                permission_value,
                "admin",
            ))

        conn.commit()
        return True

    except sqlite3.Error:
        conn.rollback()
        raise

    finally:
        conn.close()


def authenticate_user(userid, password):
    """
    아이디와 비밀번호를 확인합니다.

    기존 평문 비밀번호로 로그인에 성공하면
    해당 비밀번호를 즉시 scrypt 해시로 전환합니다.
    """
    userid = str(userid or "").strip()

    if not userid or not password:
        return None

    conn = get_connection()
    cur = conn.cursor()

    try:
        ensure_users_schema(conn)
        permission_column = get_permission_column(conn)

        if permission_column is None:
            raise sqlite3.OperationalError(
                "users 테이블에 role 또는 level 열이 없습니다."
            )

        sql = f"""
            SELECT
                id,
                userid,
                password,
                name,
                {permission_column} AS permission
            FROM users
            WHERE userid = ?
        """

        cur.execute(sql, (userid,))
        row = cur.fetchone()

        if row is None:
            return None

        stored_password = row["password"]

        if not verify_password(password, stored_password):
            return None

        # 기존 평문 비밀번호는 로그인 성공 즉시 해시로 전환
        if not is_hashed_password(stored_password):
            cur.execute("""
                UPDATE users
                SET password = ?
                WHERE id = ?
            """, (
                hash_password(password),
                row["id"],
            ))
            conn.commit()

        permission = str(row["permission"] or "").strip()

        if permission.upper() == "ADMIN":
            role = "admin"
            level = "ADMIN"
        else:
            role = "user"
            level = "USER"

        return {
            "id": row["id"],
            "userid": row["userid"],
            "name": row["name"],
            "role": role,
            "level": level,
        }

    finally:
        conn.close()


if __name__ == "__main__":
    create_tables()
    reset_admin_password("12345678")

    print("church.db 생성 및 테이블 준비 완료")
    print("선교 회계와 건축 회계 초기 항목 준비 완료")
    print("관리자 계정을 초기화했습니다.")
    print("아이디: admin")
    print("임시 비밀번호: 12345678")
    print("로그인 후 반드시 새 비밀번호로 변경하십시오.")

import os
import time
import random
import sqlite3
import hashlib
import secrets
import threading
from datetime import datetime

import requests

from flask import (
    Flask,
    request,
    jsonify,
    session,
    redirect,
    render_template_string,
)

# ============================================================
# CONFIGURATION
# ============================================================

DB_NAME = os.environ.get("DB_NAME", "aviator_live.db")

BETTING_WINDOW = 7.0
MAX_WITHDRAWAL = 250000.0
CRASH_DISPLAY_TIME = 2.5
STARTING_BALANCE = 0.0

# ============================================================
# NEXUS PAY CONFIGURATION
# ============================================================

NEXUS_PAY_API_KEY = os.environ.get("NEXUS_PAY_API_KEY", "")

NEXUS_PAY_BASE_URL = os.environ.get(
    "NEXUS_PAY_BASE_URL",
    "https://makamescopay.com"
).rstrip("/")

try:
    NEXUS_PAY_TIMEOUT = int(
        os.environ.get("NEXUS_PAY_TIMEOUT", "30")
    )
except ValueError:
    NEXUS_PAY_TIMEOUT = 30

# ============================================================
# FLASK APPLICATION
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    secrets.token_hex(32)
)

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
)

GAME_LOCK = threading.RLock()

# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(
        DB_NAME,
        timeout=10,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password):
    return hashlib.sha256(
        password.encode("utf-8")
    ).hexdigest()


def verify_password(password, hashed):
    return secrets.compare_digest(
        hash_password(password),
        hashed
    )


def init_db():
    conn = get_db()

    try:
        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                phone_number TEXT UNIQUE NOT NULL,
                balance REAL NOT NULL DEFAULT 0.0,
                role TEXT NOT NULL DEFAULT 'USER',
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                crash_point REAL NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                round_id INTEGER NOT NULL,
                bet_number INTEGER NOT NULL,
                amount REAL NOT NULL,
                auto_cashout REAL,
                cashout_multiplier REAL,
                winnings REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,

                UNIQUE(username, round_id, bet_number)
            )
        """)

        # Nexus Pay deposits.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS nexus_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                username TEXT NOT NULL,
                phone_number TEXT NOT NULL,

                amount REAL NOT NULL,

                reference TEXT UNIQUE NOT NULL,

                provider_transaction_id TEXT,

                status TEXT NOT NULL DEFAULT 'PENDING',

                provider_message TEXT,

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                credited_at TEXT
            )
        """)

        # Withdrawal records.
        #
        # The current version records withdrawals instead of pretending
        # that money has actually been sent. A real Nexus B2C/disbursement
        # implementation should be connected here using the provider's
        # documented B2C endpoint.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                username TEXT NOT NULL,
                phone_number TEXT NOT NULL,

                amount REAL NOT NULL,

                status TEXT NOT NULL DEFAULT 'PENDING',

                provider_transaction_id TEXT,
                provider_message TEXT,

                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                completed_at TEXT
            )
        """)

        # Create administrator account if it does not exist.
        admin = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            ("admin",)
        ).fetchone()

        if not admin:
            conn.execute("""
                INSERT INTO users
                (
                    username,
                    password,
                    phone_number,
                    balance,
                    role,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                "admin",
                hash_password("admin123"),
                "254700000000",
                10000.0,
                "ADMIN",
                datetime.now().isoformat()
            ))
        else:
            conn.execute(
                "UPDATE users SET role='ADMIN' WHERE username=?",
                ("admin",)
            )

        conn.commit()

    finally:
        conn.close()


# ============================================================
# GAME ENGINE
# ============================================================

def generate_crash_point():
    value = random.random()

    if value < 0.05:
        return 1.00

    if value < 0.25:
        return round(
            random.uniform(1.01, 1.50),
            2
        )

    if value < 0.65:
        return round(
            random.uniform(1.51, 3.50),
            2
        )

    if value < 0.90:
        return round(
            random.uniform(3.51, 15.00),
            2
        )

    return round(
        random.uniform(15.01, 150.00),
        2
    )


def calculate_multiplier(elapsed):
    multiplier = 1.0 + (elapsed * 0.22)

    if multiplier > 2.5:
        multiplier += (
            ((elapsed - 6.0) ** 1.25) * 0.05
        )

    return round(
        max(multiplier, 1.0),
        2
    )


GAME = {
    "round_id": None,
    "crash_point": None,

    "next_crash_point_1": generate_crash_point(),
    "next_crash_point_2": generate_crash_point(),

    "status": "BETTING",

    "betting_start": None,
    "run_start": None,

    "current_multiplier": 1.00,

    "crash_time": None,

    "bot_bets": []
}


FAKE_USERS = [
    "***1",
    "***2",
    "***3",
    "***4",
    "***5",
    "***6",
    "***7",
    "***8",
    "***9",
    "alex***",
    "brian***",
    "coll***",
    "david***",
    "eric***",
    "frank***",
    "grace***",
    "harr***",
    "ian***",
    "john***",
    "kevin***",
    "lucy***",
    "mike***",
    "nick***",
    "oliver***",
    "peter***",
    "queen***",
    "ray***",
    "sam***",
    "tom***",
    "victor***",
    "wendy***",
    "kelv***",
    "sylv***",
    "mash***",
    "kip***",
    "wanj***",
    "njeri***",
    "ochi***",
    "otien***",
    "maina***",
    "chep***",
    "kiprot***",
    "kibet***",
    "baras***"
]


def generate_bot_bets():
    bets = []

    count = random.randint(35, 65)

    selected_users = random.choices(
        FAKE_USERS,
        k=count
    )

    for i, user in enumerate(selected_users):
        masked = (
            user[:3]
            + "***"
            + str(random.randint(0, 9))
        )

        amount = round(
            random.choice([
                20,
                50,
                100,
                200,
                500,
                1000,
                2500,
                5000,
                10000
            ]),
            2
        )

        target_cashout = (
            round(
                random.uniform(1.10, 6.00),
                2
            )
            if random.random() > 0.20
            else None
        )

        bets.append({
            "id": f"bot_{i}",
            "username": masked,
            "amount": amount,
            "auto_cashout": target_cashout,
            "status": "ACTIVE",
            "cashout_multiplier": None,
            "winnings": 0.0
        })

    return bets


def create_round_locked():
    crash_point = GAME["next_crash_point_1"]

    GAME["next_crash_point_1"] = (
        GAME["next_crash_point_2"]
    )

    GAME["next_crash_point_2"] = (
        generate_crash_point()
    )

    now = datetime.now().isoformat()

    conn = get_db()

    try:
        cursor = conn.execute(
            """
            INSERT INTO rounds
            (
                crash_point,
                started_at
            )
            VALUES (?, ?)
            """,
            (
                crash_point,
                now
            )
        )

        round_id = cursor.lastrowid

        conn.commit()

    finally:
        conn.close()

    GAME["round_id"] = round_id
    GAME["crash_point"] = crash_point
    GAME["status"] = "BETTING"
    GAME["betting_start"] = time.time()
    GAME["run_start"] = None
    GAME["current_multiplier"] = 1.00
    GAME["crash_time"] = None
    GAME["bot_bets"] = generate_bot_bets()


def initialize_game():
    with GAME_LOCK:
        if GAME["round_id"] is None:
            create_round_locked()


def start_running_locked():
    if GAME["status"] != "BETTING":
        return

    GAME["status"] = "RUNNING"
    GAME["run_start"] = time.time()
    GAME["current_multiplier"] = 1.00


def process_auto_cashouts_locked():
    if GAME["status"] != "RUNNING":
        return

    multiplier = GAME["current_multiplier"]
    crash_point = GAME["crash_point"]
    round_id = GAME["round_id"]

    if multiplier >= crash_point:
        return

    # --------------------------------------------------------
    # BOT AUTO CASHOUTS
    # --------------------------------------------------------

    for bot in GAME["bot_bets"]:
        if (
            bot["status"] == "ACTIVE"
            and bot["auto_cashout"] is not None
            and bot["auto_cashout"] <= multiplier
            and bot["auto_cashout"] < crash_point
        ):
            bot["status"] = "WON"

            bot["cashout_multiplier"] = (
                bot["auto_cashout"]
            )

            bot["winnings"] = round(
                bot["amount"]
                * bot["auto_cashout"],
                2
            )

    # Some bots manually cash out.
    for bot in GAME["bot_bets"]:
        if (
            bot["status"] == "ACTIVE"
            and bot["auto_cashout"] is None
            and multiplier > 1.30
        ):
            if random.random() < 0.05:
                bot["status"] = "WON"

                bot["cashout_multiplier"] = (
                    multiplier
                )

                bot["winnings"] = round(
                    bot["amount"] * multiplier,
                    2
                )

    # --------------------------------------------------------
    # REAL USER AUTO CASHOUTS
    # --------------------------------------------------------

    conn = get_db()

    try:
        bets = conn.execute(
            """
            SELECT *
            FROM bets
            WHERE round_id = ?
              AND status = 'ACTIVE'
              AND auto_cashout IS NOT NULL
            """,
            (round_id,)
        ).fetchall()

        for bet in bets:
            target = float(
                bet["auto_cashout"]
            )

            if (
                target <= multiplier
                and target < crash_point
            ):
                winnings = round(
                    float(bet["amount"])
                    * target,
                    2
                )

                cursor = conn.execute(
                    """
                    UPDATE bets

                    SET
                        status='WON',
                        cashout_multiplier=?,
                        winnings=?

                    WHERE id=?
                      AND status='ACTIVE'
                    """,
                    (
                        target,
                        winnings,
                        bet["id"]
                    )
                )

                if cursor.rowcount == 1:
                    conn.execute(
                        """
                        UPDATE users

                        SET balance =
                            balance + ?

                        WHERE username=?
                        """,
                        (
                            winnings,
                            bet["username"]
                        )
                    )

        conn.commit()

    finally:
        conn.close()


def crash_round_locked():
    if GAME["status"] == "CRASHED":
        return

    GAME["status"] = "CRASHED"

    GAME["current_multiplier"] = (
        GAME["crash_point"]
    )

    GAME["crash_time"] = time.time()

    for bot in GAME["bot_bets"]:
        if bot["status"] == "ACTIVE":
            bot["status"] = "LOST"

    conn = get_db()

    try:
        conn.execute(
            """
            UPDATE bets

            SET status='LOST'

            WHERE round_id=?
              AND status='ACTIVE'
            """,
            (GAME["round_id"],)
        )

        conn.execute(
            """
            UPDATE rounds

            SET ended_at=?

            WHERE id=?
            """,
            (
                datetime.now().isoformat(),
                GAME["round_id"]
            )
        )

        conn.commit()

    finally:
        conn.close()


def tick_game_locked():
    initialize_game()

    now = time.time()

    if GAME["status"] == "BETTING":

        elapsed = (
            now
            - GAME["betting_start"]
        )

        GAME["current_multiplier"] = 1.00

        if elapsed >= BETTING_WINDOW:
            start_running_locked()

    elif GAME["status"] == "RUNNING":

        elapsed = (
            now
            - GAME["run_start"]
        )

        multiplier = calculate_multiplier(
            elapsed
        )

        GAME["current_multiplier"] = (
            multiplier
        )

        process_auto_cashouts_locked()

        if multiplier >= GAME["crash_point"]:
            crash_round_locked()

    elif GAME["status"] == "CRASHED":

        if (
            GAME["crash_time"] is not None
            and (
                now
                - GAME["crash_time"]
            ) >= CRASH_DISPLAY_TIME
        ):
            create_round_locked()


def game_loop():
    while True:

        try:
            with GAME_LOCK:
                tick_game_locked()

        except Exception as exc:
            print(
                "Game engine error:",
                repr(exc)
            )

        time.sleep(0.04)


# ============================================================
# USER HELPERS
# ============================================================

def get_user(username):
    conn = get_db()

    try:
        return conn.execute(
            """
            SELECT
                username,
                phone_number,
                balance,
                role

            FROM users

            WHERE username=?
            """,
            (username,)
        ).fetchone()

    finally:
        conn.close()


def normalize_mpesa_phone(phone):
    """
    Convert common Kenyan formats to:

        2547XXXXXXXX
        2541XXXXXXXX
    """

    value = str(
        phone or ""
    ).strip()

    value = (
        value
        .replace(" ", "")
        .replace("-", "")
        .replace("(", "")
        .replace(")", "")
    )

    if value.startswith("+"):
        value = value[1:]

    if (
        value.startswith("0")
        and len(value) == 10
    ):
        value = "254" + value[1:]

    elif (
        (
            value.startswith("7")
            or value.startswith("1")
        )
        and len(value) == 9
    ):
        value = "254" + value

    valid = (
        len(value) == 12
        and value.startswith("254")
        and value[3] in ("7", "1")
        and value.isdigit()
    )

    if not valid:
        raise ValueError(
            "Enter a valid Kenyan M-Pesa number, "
            "for example 0712345678."
        )

    return value


def safe_float(value, default=None):
    try:
        number = float(value)

        if number != number:
            return default

        return number

    except (
        TypeError,
        ValueError
    ):
        return default


# ============================================================
# NEXUS PAY
# ============================================================

def nexus_headers():
    if not NEXUS_PAY_API_KEY:
        raise RuntimeError(
            "NEXUS_PAY_API_KEY is not configured."
        )

    return {
        "X-API-Key": NEXUS_PAY_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }


def nexus_stk_push(
    phone_number,
    amount,
    username
):
    """
    Send an M-Pesa STK Push through Nexus Pay.

    Nexus Pay published request:

        POST /api/payments/stkpush

    JSON:

        {
            "phoneNumber": "254712345678",
            "amount": 500,
            "accountReference": "ORDER-001",
            "transactionDesc": "Payment for order"
        }
    """

    phone = normalize_mpesa_phone(
        phone_number
    )

    amount = safe_float(amount)

    if amount is None or amount <= 0:
        raise ValueError(
            "Deposit amount must be greater than zero."
        )

    if amount != round(amount):
        raise ValueError(
            "Deposit amount must be a whole KES amount."
        )

    amount = int(round(amount))

    # Short unique reference.
    reference = (
        f"AV-{username[:8]}-"
        f"{int(time.time())}-"
        f"{secrets.token_hex(3).upper()}"
    )

    payload = {
        "phoneNumber": phone,
        "amount": amount,
        "accountReference": reference,
        "transactionDesc": "Aviator deposit"
    }

    url = (
        f"{NEXUS_PAY_BASE_URL}"
        "/api/payments/stkpush"
    )

    response = requests.post(
        url,
        json=payload,
        headers=nexus_headers(),
        timeout=NEXUS_PAY_TIMEOUT
    )

    response.raise_for_status()

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            "Nexus Pay returned an invalid response."
        )

    # Keep the entire provider response useful,
    # while trying several common identifiers.
    provider_transaction_id = (
        data.get("transactionId")
        or data.get("transaction_id")
        or data.get("checkoutRequestId")
        or data.get("checkout_request_id")
        or data.get("id")
    )

    # Some APIs return success as a boolean,
    # others return a status/code.
    success = data.get("success")

    if success is False:
        message = (
            data.get("message")
            or data.get("error")
            or data.get("description")
            or "Nexus Pay rejected the payment request."
        )

        raise RuntimeError(str(message))

    status_value = str(
        data.get("status", "")
    ).upper()

    if status_value in (
        "FAILED",
        "FAILURE",
        "ERROR",
        "REJECTED"
    ):
        message = (
            data.get("message")
            or data.get("error")
            or data.get("description")
            or "Nexus Pay rejected the payment request."
        )

        raise RuntimeError(str(message))

    now = datetime.now().isoformat()

    conn = get_db()

    try:
        conn.execute(
            """
            INSERT INTO nexus_transactions
            (
                username,
                phone_number,
                amount,
                reference,
                provider_transaction_id,
                status,
                provider_message,
                created_at,
                updated_at
            )

            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                phone,
                float(amount),
                reference,
                (
                    str(provider_transaction_id)
                    if provider_transaction_id
                    else None
                ),
                "PENDING",
                (
                    data.get("message")
                    or data.get("description")
                    or "STK Push initiated."
                ),
                now,
                now
            )
        )

        conn.commit()

    finally:
        conn.close()

    return {
        "reference": reference,
        "provider_transaction_id":
            provider_transaction_id,
        "message":
            data.get("message")
            or data.get("description")
            or "M-Pesa prompt sent.",
        "provider_response": data
    }


# ============================================================
# NEXUS TRANSACTION HELPERS
# ============================================================

def get_nexus_transaction(
    reference,
    username
):
    conn = get_db()

    try:
        return conn.execute(
            """
            SELECT *

            FROM nexus_transactions

            WHERE reference=?
              AND username=?
            """,
            (
                reference,
                username
            )
        ).fetchone()

    finally:
        conn.close()


def credit_nexus_transaction(
    reference
):
    """
    IMPORTANT:

    This function is intentionally not exposed as a public
    "mark paid" endpoint.

    It can be called only after your provider's documented
    confirmation mechanism has verified the payment.

    It is idempotent: the same transaction cannot be credited twice.
    """

    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        tx = conn.execute(
            """
            SELECT *

            FROM nexus_transactions

            WHERE reference=?
            """,
            (reference,)
        ).fetchone()

        if not tx:
            conn.rollback()
            return False, "Transaction not found."

        if tx["status"] == "SUCCESS":
            conn.commit()
            return True, "Already credited."

        amount = float(tx["amount"])

        now = datetime.now().isoformat()

        cursor = conn.execute(
            """
            UPDATE nexus_transactions

            SET
                status='SUCCESS',
                updated_at=?,
                credited_at=?

            WHERE reference=?
              AND status='PENDING'
            """,
            (
                now,
                now,
                reference
            )
        )

        if cursor.rowcount != 1:
            conn.rollback()
            return False, "Transaction could not be credited."

        conn.execute(
            """
            UPDATE users

            SET balance=balance+?

            WHERE username=?
            """,
            (
                amount,
                tx["username"]
            )
        )

        conn.commit()

        return True, (
            f"Deposit of KES "
            f"{amount:,.2f} credited."
        )

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# ============================================================
# BALANCE / BET FUNCTIONS
# ============================================================

def deduct_balance(
    username,
    amount
):
    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        cursor = conn.execute(
            """
            UPDATE users

            SET balance=balance-?

            WHERE username=?
              AND balance>=?
            """,
            (
                amount,
                username,
                amount
            )
        )

        if cursor.rowcount != 1:
            conn.rollback()
            return False

        conn.commit()

        return True

    except Exception:
        conn.rollback()
        return False

    finally:
        conn.close()


def place_bet_for_user(
    username,
    bet_number,
    amount,
    auto_cashout
):
    with GAME_LOCK:

        tick_game_locked()

        if bet_number not in (1, 2):
            return False, "Invalid bet slot."

        if GAME["status"] != "BETTING":
            return False, "Betting closed for this round."

        amount = safe_float(amount)

        if amount is None:
            return False, "Invalid amount."

        if amount <= 0:
            return False, "Amount must be greater than zero."

        if auto_cashout in (
            None,
            "",
            False
        ):
            auto_cashout = None

        else:
            auto_cashout = safe_float(
                auto_cashout
            )

            if auto_cashout is None:
                return False, "Invalid auto cashout."

            if auto_cashout < 1.01:
                return False, (
                    "Auto cashout minimum is 1.01x."
                )

        conn = get_db()

        try:
            conn.execute("BEGIN IMMEDIATE")

            existing = conn.execute(
                """
                SELECT id

                FROM bets

                WHERE username=?
                  AND round_id=?
                  AND bet_number=?
                """,
                (
                    username,
                    GAME["round_id"],
                    bet_number
                )
            ).fetchone()

            if existing:
                conn.rollback()

                return False, (
                    f"Bet {bet_number} already placed."
                )

            user = conn.execute(
                """
                SELECT balance

                FROM users

                WHERE username=?
                """,
                (username,)
            ).fetchone()

            if not user:
                conn.rollback()
                return False, "User not found."

            if float(user["balance"]) < amount:
                conn.rollback()
                return False, "Insufficient balance."

            cursor = conn.execute(
                """
                UPDATE users

                SET balance=balance-?

                WHERE username=?
                  AND balance>=?
                """,
                (
                    amount,
                    username,
                    amount
                )
            )

            if cursor.rowcount != 1:
                conn.rollback()
                return False, "Insufficient balance."

            conn.execute(
                """
                INSERT INTO bets
                (
                    username,
                    round_id,
                    bet_number,
                    amount,
                    auto_cashout,
                    winnings,
                    status,
                    created_at
                )

                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    GAME["round_id"],
                    bet_number,
                    amount,
                    auto_cashout,
                    0.0,
                    "ACTIVE",
                    datetime.now().isoformat()
                )
            )

            conn.commit()

            return True, (
                f"Bet {bet_number} placed."
            )

        except Exception as exc:
            conn.rollback()

            print(
                "Place bet error:",
                repr(exc)
            )

            return False, "Failed to place bet."

        finally:
            conn.close()


def manual_cashout(
    username,
    bet_number
):
    with GAME_LOCK:

        tick_game_locked()

        if GAME["status"] != "RUNNING":
            return False, "Flight not running."

        multiplier = GAME["current_multiplier"]

        if multiplier >= GAME["crash_point"]:
            return False, "Round crashed."

        multiplier = round(
            multiplier,
            2
        )

        conn = get_db()

        try:
            conn.execute("BEGIN IMMEDIATE")

            bet = conn.execute(
                """
                SELECT *

                FROM bets

                WHERE username=?
                  AND round_id=?
                  AND bet_number=?
                  AND status='ACTIVE'
                """,
                (
                    username,
                    GAME["round_id"],
                    bet_number
                )
            ).fetchone()

            if not bet:
                conn.rollback()
                return False, (
                    "No active bet found."
                )

            winnings = round(
                float(bet["amount"])
                * multiplier,
                2
            )

            cursor = conn.execute(
                """
                UPDATE bets

                SET
                    status='WON',
                    cashout_multiplier=?,
                    winnings=?

                WHERE id=?
                  AND status='ACTIVE'
                """,
                (
                    multiplier,
                    winnings,
                    bet["id"]
                )
            )

            if cursor.rowcount != 1:
                conn.rollback()
                return False, "Cashout failed."

            conn.execute(
                """
                UPDATE users

                SET balance=balance+?

                WHERE username=?
                """,
                (
                    winnings,
                    username
                )
            )

            conn.commit()

            return True, (
                f"Cashed out at "
                f"{multiplier:.2f}x — "
                f"KES {winnings:,.2f}"
            )

        except Exception as exc:
            conn.rollback()

            print(
                "Cashout error:",
                repr(exc)
            )

            return False, "Cashout error."

        finally:
            conn.close()


# ============================================================
# AUTHENTICATION
# ============================================================

@app.route("/")
def index():

    if "username" not in session:
        return redirect("/login")

    return render_template_string(
        HTML,
        username=session["username"]
    )


@app.route(
    "/login",
    methods=["GET", "POST"]
)
def login():

    error = None

    if request.method == "POST":

        username = (
            request.form
            .get("username", "")
            .strip()
        )

        password = request.form.get(
            "password",
            ""
        )

        conn = get_db()

        try:
            user = conn.execute(
                """
                SELECT *

                FROM users

                WHERE username=?
                """,
                (username,)
            ).fetchone()

        finally:
            conn.close()

        if (
            user
            and verify_password(
                password,
                user["password"]
            )
        ):
            session.clear()

            session["username"] = (
                user["username"]
            )

            session["role"] = (
                user["role"]
            )

            return redirect("/")

        error = "Invalid credentials."

    return render_template_string(
        LOGIN_HTML,
        error=error
    )


@app.route(
    "/register",
    methods=["GET", "POST"]
)
def register():

    error = None

    if request.method == "POST":

        username = (
            request.form
            .get("username", "")
            .strip()
        )

        password = request.form.get(
            "password",
            ""
        )

        phone = (
            request.form
            .get("phone_number", "")
            .strip()
        )

        if not username:
            error = "Username is required."

        elif len(username) < 3:
            error = (
                "Username must be at least 3 characters."
            )

        elif not password:
            error = "Password is required."

        elif len(password) < 6:
            error = (
                "Password must be at least 6 characters."
            )

        else:
            try:
                phone = normalize_mpesa_phone(
                    phone
                )

            except ValueError as exc:
                error = str(exc)

        if not error:

            conn = get_db()

            try:
                conn.execute(
                    """
                    INSERT INTO users
                    (
                        username,
                        password,
                        phone_number,
                        balance,
                        role,
                        created_at
                    )

                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        username,
                        hash_password(password),
                        phone,
                        STARTING_BALANCE,
                        "USER",
                        datetime.now().isoformat()
                    )
                )

                conn.commit()

                session.clear()

                session["username"] = (
                    username
                )

                session["role"] = "USER"

                return redirect("/")

            except sqlite3.IntegrityError:
                error = (
                    "Username or phone number "
                    "is already registered."
                )

            finally:
                conn.close()

    return render_template_string(
        REGISTER_HTML,
        error=error
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ============================================================
# GAME API
# ============================================================

@app.route("/api/state")
def api_state():

    if "username" not in session:
        return jsonify({
            "error": "Unauthorized"
        }), 401

    with GAME_LOCK:

        tick_game_locked()

        username = session["username"]

        user = get_user(username)

        if not user:
            session.clear()

            return jsonify({
                "error": "User not found."
            }), 401

        is_admin = (
            user["role"] == "ADMIN"
        )

        conn = get_db()

        try:

            history_rows = conn.execute(
                """
                SELECT crash_point

                FROM rounds

                WHERE ended_at IS NOT NULL

                ORDER BY id DESC

                LIMIT 20
                """
            ).fetchall()

            history = [
                float(row["crash_point"])
                for row in reversed(
                    history_rows
                )
            ]

            bet_rows = conn.execute(
                """
                SELECT
                    bet_number,
                    amount,
                    auto_cashout,
                    cashout_multiplier,
                    winnings,
                    status

                FROM bets

                WHERE username=?
                  AND round_id=?
                """,
                (
                    username,
                    GAME["round_id"]
                )
            ).fetchall()

        finally:
            conn.close()

        user_bets = {}

        all_live_bets = []

        # Bot feed.
        for bot in GAME["bot_bets"]:
            all_live_bets.append(bot)

        # Real user bets.
        for bet in bet_rows:

            user_bets[
                str(bet["bet_number"])
            ] = dict(bet)

            all_live_bets.append({
                "id":
                    f"real_{bet['bet_number']}",

                "username":
                    username,

                "amount":
                    float(bet["amount"]),

                "auto_cashout":
                    bet["auto_cashout"],

                "status":
                    bet["status"],

                "cashout_multiplier":
                    bet["cashout_multiplier"],

                "winnings":
                    float(bet["winnings"])
            })

        all_live_bets.sort(
            key=lambda item:
                0
                if item["status"] == "ACTIVE"
                else 1
        )

        return jsonify({

            "status":
                GAME["status"],

            "multiplier":
                GAME["current_multiplier"],

            "crash_point":
                (
                    GAME["crash_point"]
                    if GAME["status"] == "CRASHED"
                    else None
                ),

            "next_crash_point_1":
                (
                    GAME["next_crash_point_1"]
                    if is_admin
                    else None
                ),

            "next_crash_point_2":
                (
                    GAME["next_crash_point_2"]
                    if is_admin
                    else None
                ),

            "is_admin":
                is_admin,

            "balance":
                float(user["balance"]),

            "bets":
                user_bets,

            "live_feed":
                all_live_bets,

            "history":
                history,

            "round_id":
                GAME["round_id"]
        })


@app.route(
    "/api/bet",
    methods=["POST"]
)
def api_bet():

    if "username" not in session:
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        }), 401

    data = request.get_json(
        silent=True
    ) or {}

    bet_number = data.get(
        "bet_number",
        1
    )

    try:
        bet_number = int(bet_number)

    except (
        TypeError,
        ValueError
    ):
        return jsonify({
            "success": False,
            "message": "Invalid bet number."
        })

    success, message = place_bet_for_user(
        session["username"],
        bet_number,
        data.get("amount", 0),
        data.get("auto_cashout")
    )

    return jsonify({
        "success": success,
        "message": message
    })


@app.route(
    "/api/cashout",
    methods=["POST"]
)
def api_cashout():

    if "username" not in session:
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        }), 401

    data = request.get_json(
        silent=True
    ) or {}

    try:
        bet_number = int(
            data.get(
                "bet_number",
                1
            )
        )

    except (
        TypeError,
        ValueError
    ):
        return jsonify({
            "success": False,
            "message": "Invalid bet number."
        })

    success, message = manual_cashout(
        session["username"],
        bet_number
    )

    return jsonify({
        "success": success,
        "message": message
    })


# ============================================================
# NEXUS PAY STK PUSH
# ============================================================

@app.route(
    "/api/nexus/stkpush",
    methods=["POST"]
)
def api_nexus_stkpush():

    if "username" not in session:
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        }), 401

    data = request.get_json(
        silent=True
    ) or {}

    username = session["username"]

    user = get_user(username)

    if not user:
        return jsonify({
            "success": False,
            "message": "User not found."
        }), 401

    amount = safe_float(
        data.get("amount")
    )

    if amount is None:
        return jsonify({
            "success": False,
            "message": "Invalid amount."
        })

    if amount < 1:
        return jsonify({
            "success": False,
            "message": (
                "Minimum deposit is KES 1."
            )
        })

    phone_input = (
        data.get("phone")
        or user["phone_number"]
    )

    try:
        phone = normalize_mpesa_phone(
            phone_input
        )

        result = nexus_stk_push(
            phone,
            amount,
            username
        )

        return jsonify({
            "success": True,

            "message":
                result["message"],

            "reference":
                result["reference"],

            "provider_transaction_id":
                result["provider_transaction_id"]
        })

    except ValueError as exc:

        return jsonify({
            "success": False,
            "message": str(exc)
        })

    except requests.Timeout:

        return jsonify({
            "success": False,
            "message":
                "Nexus Pay request timed out."
        }), 504

    except requests.HTTPError as exc:

        print(
            "Nexus Pay HTTP error:",
            repr(exc)
        )

        response = getattr(
            exc,
            "response",
            None
        )

        provider_message = None

        if response is not None:
            try:
                payload = response.json()

                provider_message = (
                    payload.get("message")
                    or payload.get("error")
                    or payload.get("description")
                )

            except Exception:
                provider_message = None

        return jsonify({
            "success": False,
            "message":
                provider_message
                or "Nexus Pay rejected the request."
        }), 502

    except requests.RequestException as exc:

        print(
            "Nexus Pay connection error:",
            repr(exc)
        )

        return jsonify({
            "success": False,
            "message":
                "Could not connect to Nexus Pay."
        }), 502

    except Exception as exc:

        print(
            "Nexus Pay STK error:",
            repr(exc)
        )

        return jsonify({
            "success": False,
            "message": str(exc)
        }), 500


# ============================================================
# NEXUS TRANSACTION STATUS
# ============================================================

@app.route(
    "/api/nexus/status/<path:reference>",
    methods=["GET"]
)
def api_nexus_status(reference):

    if "username" not in session:
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        }), 401

    tx = get_nexus_transaction(
        reference,
        session["username"]
    )

    if not tx:
        return jsonify({
            "success": False,
            "message": "Transaction not found."
        }), 404

    return jsonify({
        "success": True,

        "reference":
            tx["reference"],

        "status":
            tx["status"],

        "amount":
            float(tx["amount"]),

        "message":
            tx["provider_message"]
            or "Waiting for payment confirmation.",

        "provider_transaction_id":
            tx["provider_transaction_id"],

        "created_at":
            tx["created_at"],

        "credited_at":
            tx["credited_at"]
    })


# ============================================================
# NEXUS CALLBACK PLACEHOLDER
# ============================================================
#
# Do NOT automatically credit payments from an undocumented
# request.
#
# If Nexus gives you an official webhook/callback specification,
# replace this route with the exact verification procedure from
# their documentation.
#
# ============================================================

@app.route(
    "/api/nexus/callback",
    methods=["POST"]
)
def nexus_callback():

    return jsonify({
        "success": False,
        "message": (
            "Nexus callback verification is not configured. "
            "No account balance was changed."
        )
    }), 501


# ============================================================
# ADMIN PAYMENT CONFIRMATION
# ============================================================
#
# This is intentionally protected by the ADMIN role.
#
# It can be used while testing if you have independently verified
# the payment in Nexus Pay.
#
# For production, replace this manual confirmation with Nexus's
# official webhook/status verification once you have that API
# documentation.
#
# ============================================================

@app.route(
    "/api/admin/nexus/confirm",
    methods=["POST"]
)
def admin_confirm_nexus():

    if "username" not in session:
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        }), 401

    if session.get("role") != "ADMIN":
        return jsonify({
            "success": False,
            "message": "Admin access required."
        }), 403

    data = request.get_json(
        silent=True
    ) or {}

    reference = str(
        data.get("reference", "")
    ).strip()

    if not reference:
        return jsonify({
            "success": False,
            "message": "Reference is required."
        })

    try:
        success, message = (
            credit_nexus_transaction(
                reference
            )
        )

        return jsonify({
            "success": success,
            "message": message
        })

    except Exception as exc:

        print(
            "Nexus confirmation error:",
            repr(exc)
        )

        return jsonify({
            "success": False,
            "message": "Confirmation failed."
        }), 500


# ============================================================
# WITHDRAWALS
# ============================================================

@app.route(
    "/api/withdraw",
    methods=["POST"]
)
def api_withdraw():

    if "username" not in session:
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        }), 401

    data = request.get_json(
        silent=True
    ) or {}

    amount = safe_float(
        data.get("amount")
    )

    if amount is None:
        return jsonify({
            "success": False,
            "message": "Invalid amount."
        })

    if amount <= 0:
        return jsonify({
            "success": False,
            "message": (
                "Withdrawal amount "
                "must be greater than zero."
            )
        })

    if amount > MAX_WITHDRAWAL:
        return jsonify({
            "success": False,
            "message": (
                f"Maximum withdrawal is "
                f"KES {MAX_WITHDRAWAL:,.2f}."
            )
        })

    username = session["username"]

    user = get_user(username)

    if not user:
        return jsonify({
            "success": False,
            "message": "User not found."
        }), 404

    # Reserve the balance atomically.
    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        cursor = conn.execute(
            """
            UPDATE users

            SET balance=balance-?

            WHERE username=?
              AND balance>=?
            """,
            (
                amount,
                username,
                amount
            )
        )

        if cursor.rowcount != 1:
            conn.rollback()

            return jsonify({
                "success": False,
                "message": "Insufficient balance."
            })

        now = datetime.now().isoformat()

        cursor = conn.execute(
            """
            INSERT INTO withdrawals
            (
                username,
                phone_number,
                amount,
                status,
                provider_message,
                created_at,
                updated_at
            )

            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                user["phone_number"],
                amount,
                "PENDING",
                "Withdrawal awaiting processing.",
                now,
                now
            )
        )

        withdrawal_id = cursor.lastrowid

        conn.commit()

    except Exception as exc:

        conn.rollback()

        print(
            "Withdrawal error:",
            repr(exc)
        )

        return jsonify({
            "success": False,
            "message": "Withdrawal request failed."
        }), 500

    finally:
        conn.close()

    return jsonify({
        "success": True,
        "message": (
            f"Withdrawal of KES "
            f"{amount:,.2f} has been submitted "
            f"for processing."
        ),
        "withdrawal_id": withdrawal_id
    })


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "game_status": GAME["status"],
        "nexus_configured": bool(
            NEXUS_PAY_API_KEY
        )
    })


# ============================================================
# LOGIN PAGE
# ============================================================

LOGIN_HTML = r"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>Aviator - Login</title>

<style>

body {
    margin: 0;
    min-height: 100vh;

    display: flex;
    justify-content: center;
    align-items: center;

    background: #0e1015;

    font-family: sans-serif;
    color: #fff;
}

.box {
    width: 100%;
    max-width: 360px;

    padding: 30px;

    border-radius: 12px;

    background: #181c24;

    border: 1px solid #282f3d;

    box-sizing: border-box;
}

h2 {
    text-align: center;
    color: #28a745;
    margin-top: 0;
}

input {
    width: 100%;

    padding: 12px;

    margin: 8px 0 16px;

    border-radius: 6px;

    border: 1px solid #282f3d;

    background: #0e1015;

    color: #fff;

    box-sizing: border-box;
}

button {
    width: 100%;

    padding: 12px;

    border: none;

    border-radius: 6px;

    background: #28a745;

    color: #fff;

    font-weight: bold;

    cursor: pointer;

    font-size: 16px;
}

.error {
    color: #e53e3e;

    text-align: center;

    margin-bottom: 12px;

    font-size: 14px;
}

a {
    color: #28a745;

    text-decoration: none;
}

</style>

</head>

<body>

<div class="box">

    <h2>✈️ AVIATOR LOGIN</h2>

    {% if error %}
        <div class="error">
            {{ error }}
        </div>
    {% endif %}

    <form method="POST">

        <label>Username</label>

        <input
            type="text"
            name="username"
            required
            autocomplete="username"
        >

        <label>Password</label>

        <input
            type="password"
            name="password"
            required
            autocomplete="current-password"
        >

        <button type="submit">
            LOG IN
        </button>

    </form>

    <p
        style="
            text-align:center;
            font-size:14px;
            color:#888;
        "
    >
        No account?
        <a href="/register">
            Register
        </a>
    </p>

</div>

</body>

</html>
"""


# ============================================================
# REGISTER PAGE
# ============================================================

REGISTER_HTML = r"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>Aviator - Register</title>

<style>

body {
    margin: 0;

    min-height: 100vh;

    display: flex;

    justify-content: center;

    align-items: center;

    background: #0e1015;

    font-family: sans-serif;

    color: #fff;
}

.box {
    width: 100%;

    max-width: 360px;

    padding: 30px;

    border-radius: 12px;

    background: #181c24;

    border: 1px solid #282f3d;

    box-sizing: border-box;
}

h2 {
    text-align: center;

    color: #28a745;

    margin-top: 0;
}

input {
    width: 100%;

    padding: 12px;

    margin: 8px 0 16px;

    border-radius: 6px;

    border: 1px solid #282f3d;

    background: #0e1015;

    color: #fff;

    box-sizing: border-box;
}

button {
    width: 100%;

    padding: 12px;

    border: none;

    border-radius: 6px;

    background: #28a745;

    color: #fff;

    font-weight: bold;

    cursor: pointer;

    font-size: 16px;
}

.error {
    color: #e53e3e;

    text-align: center;

    margin-bottom: 12px;

    font-size: 14px;
}

a {
    color: #28a745;

    text-decoration: none;
}

</style>

</head>

<body>

<div class="box">

    <h2>✈️ CREATE ACCOUNT</h2>

    {% if error %}
        <div class="error">
            {{ error }}
        </div>
    {% endif %}

    <form method="POST">

        <label>Username</label>

        <input
            type="text"
            name="username"
            required
            autocomplete="username"
        >

        <label>
            M-Pesa Phone Number
        </label>

        <input
            type="text"
            name="phone_number"
            placeholder="0712345678"
            required
        >

        <label>Password</label>

        <input
            type="password"
            name="password"
            required
            minlength="6"
            autocomplete="new-password"
        >

        <button type="submit">
            SIGN UP
        </button>

    </form>

    <p
        style="
            text-align:center;
            font-size:14px;
            color:#888;
        "
    >
        Already have an account?

        <a href="/login">
            Login
        </a>
    </p>

</div>

</body>

</html>
"""


# ============================================================
# MAIN GAME HTML
# ============================================================

HTML = r"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>Aviator Game Interface</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;

    padding: 0;

    background-color: #000;

    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        Roboto,
        sans-serif;

    color: #fff;

    user-select: none;
}

/* ==========================================================
   HEADER
   ========================================================== */

.header {
    display: flex;

    justify-content: space-between;

    align-items: center;

    background: #1b1c20;

    padding: 10px 16px;

    border-bottom: 1px solid #2a2b30;

    gap: 12px;
}

.brand {
    font-size: 20px;

    font-weight: 900;

    color: #28a745;

    letter-spacing: 1px;
}

.wallet {
    display: flex;

    align-items: center;

    gap: 10px;

    flex-wrap: wrap;

    justify-content: flex-end;
}

.balance {
    color: #28a745;

    font-weight: bold;

    font-size: 16px;
}

.deposit-btn,
.withdraw-btn {
    color: #fff;

    font-weight: bold;

    border: none;

    padding: 8px 16px;

    border-radius: 20px;

    cursor: pointer;

    font-size: 13px;
}

.deposit-btn {
    background: #28a745;
}

.withdraw-btn {
    background: #007bff;
}

/* ==========================================================
   ADMIN
   ========================================================== */

.admin-banner {
    display: none;

    background: #d97706;

    color: #fff;

    text-align: center;

    font-weight: bold;

    padding: 6px;

    font-size: 14px;

    border-bottom: 2px solid #b45309;
}

/* ==========================================================
   MODAL
   ========================================================== */

.mpesa-modal {
    display: none;

    position: fixed;

    inset: 0;

    z-index: 9999;

    background: rgba(0, 0, 0, .72);

    align-items: center;

    justify-content: center;
}

.mpesa-card {
    width: min(420px, 92vw);

    background: #17191f;

    border: 1px solid #30343e;

    border-radius: 16px;

    padding: 22px;

    box-shadow:
        0 20px 70px
        rgba(0, 0, 0, .5);
}

.mpesa-card h3 {
    margin: 0 0 6px;

    color: #28a745;
}

.mpesa-card p {
    color: #aaa;

    font-size: 13px;

    line-height: 1.45;
}

.mpesa-card label {
    display: block;

    color: #ddd;

    font-size: 12px;

    margin: 14px 0 6px;
}

.mpesa-card input {
    width: 100%;

    padding: 12px;

    border-radius: 8px;

    border: 1px solid #333844;

    background: #0d0f13;

    color: #fff;
}

.mpesa-actions {
    display: flex;

    gap: 10px;

    margin-top: 16px;
}

.mpesa-actions button {
    flex: 1;

    padding: 12px;

    border: 0;

    border-radius: 8px;

    cursor: pointer;

    font-weight: 700;
}

.mpesa-pay {
    background: #28a745;

    color: #fff;
}

.mpesa-cancel {
    background: #30343e;

    color: #fff;
}

.mpesa-status {
    margin-top: 12px;

    min-height: 18px;

    font-size: 12px;

    color: #bbb;
}

/* ==========================================================
   MAIN
   ========================================================== */

.main-layout {
    max-width: 1200px;

    margin: 0 auto;

    padding: 10px;
}

/* ==========================================================
   HISTORY
   ========================================================== */

.history-line {
    display: flex;

    gap: 8px;

    overflow-x: auto;

    padding: 6px 0;

    margin-bottom: 8px;

    scrollbar-width: none;
}

.history-line::-webkit-scrollbar {
    display: none;
}

.hist-item {
    font-size: 13px;

    font-weight: 700;

    padding: 2px 8px;

    border-radius: 10px;

    white-space: nowrap;
}

.hist-blue {
    color: #3498db;
}

.hist-purple {
    color: #9b59b6;
}

.hist-pink {
    color: #e91e63;
}

/* ==========================================================
   GAME STAGE
   ========================================================== */

.stage {
    position: relative;

    width: 100%;

    height: 320px;

    background:
        radial-gradient(
            circle at 10% 90%,
            #20081e 0%,
            #0d060e 100%
        );

    border-radius: 12px 12px 0 0;

    overflow: hidden;

    border: 1px solid #2a2c33;
}

canvas {
    width: 100%;

    height: 100%;

    display: block;
}

.multiplier-overlay {
    position: absolute;

    top: 40%;

    left: 50%;

    transform:
        translate(-50%, -50%);

    font-size: 64px;

    font-weight: 900;

    text-align: center;

    color: #fff;

    z-index: 10;

    text-shadow:
        0 0 15px
        rgba(0, 0, 0, .8);

    white-space: pre-line;
}

.crashed-text {
    color: #e53e3e;

    text-shadow:
        0 0 10px
        rgba(229, 62, 62, .6);
}

/* ==========================================================
   CONTROLS
   ========================================================== */

.panels-container {
    display: grid;

    grid-template-columns: 1fr 1fr;

    gap: 10px;

    background: #141518;

    padding: 10px;

    border-radius: 0 0 12px 12px;

    border: 1px solid #2a2c33;

    border-top: none;
}

.bet-box {
    background: #1b1c21;

    border-radius: 10px;

    padding: 10px;

    border: 1px solid #25272e;
}

.toggle-bar {
    display: flex;

    justify-content: center;

    background: #0e0f12;

    border-radius: 15px;

    padding: 2px;

    margin-bottom: 8px;

    width: 160px;

    margin-left: auto;

    margin-right: auto;
}

.toggle-btn {
    flex: 1;

    text-align: center;

    padding: 4px 0;

    font-size: 12px;

    border-radius: 13px;

    cursor: pointer;

    color: #888;

    font-weight: bold;
}

.toggle-btn.active {
    background: #252830;

    color: #fff;
}

.auto-options {
    display: none;

    margin-bottom: 8px;

    gap: 8px;

    align-items: center;

    justify-content: center;
}

.auto-options.show {
    display: flex;
}

.auto-input {
    width: 80px;

    background: #0e1013;

    border: 1px solid #2a2c33;

    color: #fff;

    border-radius: 6px;

    padding: 4px;

    text-align: center;

    font-weight: bold;
}

.controls-flex {
    display: flex;

    gap: 10px;

    align-items: center;
}

.left-inputs {
    flex: 1;
}

.input-stepper {
    display: flex;

    align-items: center;

    background: #0e1013;

    border-radius: 20px;

    padding: 2px;

    border: 1px solid #2a2c33;
}

.step-btn {
    width: 32px;

    height: 32px;

    border-radius: 50%;

    border: none;

    background: #1b1c21;

    color: #888;

    font-size: 18px;

    cursor: pointer;

    display: flex;

    align-items: center;

    justify-content: center;
}

.step-btn:hover {
    color: #fff;
}

.amount-input {
    flex: 1;

    background: transparent;

    border: none;

    color: #fff;

    font-size: 16px;

    font-weight: bold;

    text-align: center;

    width: 100%;

    outline: none;
}

.quick-bets {
    display: grid;

    grid-template-columns: 1fr 1fr;

    gap: 4px;

    margin-top: 6px;
}

.q-btn {
    background: #121316;

    border: 1px solid #22242b;

    color: #aaa;

    border-radius: 10px;

    padding: 4px;

    font-size: 11px;

    cursor: pointer;

    text-align: center;
}

.q-btn:hover {
    color: #fff;

    border-color: #444;
}

.action-btn {
    flex: 1.1;

    height: 82px;

    border: none;

    border-radius: 12px;

    font-weight: 900;

    cursor: pointer;

    display: flex;

    flex-direction: column;

    justify-content: center;

    align-items: center;

    transition: all .2s;
}

.btn-green {
    background: #28a745;

    color: #fff;
}

.btn-green:hover {
    background: #218838;
}

.btn-orange {
    background: #d97706;

    color: #fff;
}

.btn-orange:hover {
    background: #b45309;
}

.btn-blue {
    background: #007bff;

    color: #fff;
}

.btn-disabled {
    background: #333 !important;

    color: #666 !important;

    cursor: not-allowed;
}

.btn-title {
    font-size: 18px;
}

.btn-sub {
    font-size: 16px;

    margin-top: 2px;
}

/* ==========================================================
   FEED
   ========================================================== */

.feed-section {
    margin-top: 15px;

    background: #141518;

    border-radius: 12px;

    padding: 12px;

    border: 1px solid #2a2c33;
}

.feed-header {
    font-size: 12px;

    color: #888;

    border-bottom: 1px solid #22242b;

    padding-bottom: 8px;

    margin-bottom: 8px;
}

.feed-row {
    display: flex;

    justify-content: space-between;

    gap: 10px;

    font-size: 13px;

    padding: 6px 0;

    border-bottom: 1px solid #1a1c22;
}

@media (max-width: 768px) {

    .header {
        align-items: flex-start;
    }

    .wallet {
        flex-direction: column;

        align-items: flex-end;
    }

    .panels-container {
        grid-template-columns: 1fr;
    }

    .stage {
        height: 250px;
    }

    .multiplier-overlay {
        font-size: 42px;
    }

    .feed-row {
        font-size: 11px;
    }
}

</style>

</head>

<body>

<div
    class="admin-banner"
    id="adminBanner"
>
    ⚡ ADMIN CONTROLS:

    NEXT ROUND:
    <span id="next1">-</span>

    |

    ROUND AFTER:
    <span id="next2">-</span>
</div>


<div class="header">

    <div class="brand">
        ✈️ AVIATOR
    </div>

    <div class="wallet">

        <span
            class="balance"
            id="balanceDisplay"
        >
            0.00 KES
        </span>

        <button
            class="deposit-btn"
            onclick="openDepositGateway()"
        >
            DEPOSIT
        </button>

        <button
            class="withdraw-btn"
            onclick="triggerWithdrawal()"
        >
            WITHDRAW
        </button>

        <a
            href="/logout"
            style="
                color:#e53e3e;
                text-decoration:none;
                font-size:12px;
            "
        >
            Logout
        </a>

    </div>

</div>


<!-- ========================================================
     NEXUS DEPOSIT MODAL
     ======================================================== -->

<div
    class="mpesa-modal"
    id="mpesaModal"
>

    <div class="mpesa-card">

        <h3>
            Deposit with M-Pesa
        </h3>

        <p>
            Enter your amount and M-Pesa number.
            Nexus Pay will send an M-Pesa payment
            prompt to your phone.
        </p>

        <label for="mpesaAmount">
            Amount (KES)
        </label>

        <input
            id="mpesaAmount"
            type="number"
            min="200"
            step="10"
            value="200"
        >

        <label for="mpesaPhone">
            M-Pesa phone number
        </label>

        <input
            id="mpesaPhone"
            type="tel"
            placeholder="0712345678"
        >

        <div
            class="mpesa-status"
            id="mpesaStatus"
        ></div>

        <div class="mpesa-actions">

            <button
                class="mpesa-cancel"
                onclick="closeMpesaModal()"
            >
                CANCEL
            </button>

            <button
                class="mpesa-pay"
                id="mpesaPayBtn"
                onclick="requestNexusDeposit()"
            >
                PAY
            </button>

        </div>

    </div>

</div>


<div class="main-layout">

    <!-- HISTORY -->

    <div
        class="history-line"
        id="historyLine"
    ></div>


    <!-- GAME -->

    <div class="stage">

        <canvas
            id="flightCanvas"
        ></canvas>

        <div
            class="multiplier-overlay"
            id="multiplierDisplay"
        >
            1.00x
        </div>

    </div>


    <!-- CONTROLS -->

    <div class="panels-container">

        <!-- PANEL 1 -->

        <div class="bet-box">

            <div class="toggle-bar">

                <div
                    class="toggle-btn active"
                    id="tabBet1"
                    onclick="switchTab(1, 'bet')"
                >
                    Bet
                </div>

                <div
                    class="toggle-btn"
                    id="tabAuto1"
                    onclick="switchTab(1, 'auto')"
                >
                    Auto
                </div>

            </div>


            <div
                class="auto-options"
                id="autoOpt1"
            >

                <label
                    style="
                        font-size:11px;
                        color:#aaa;
                    "
                >
                    Auto Cashout x
                </label>

                <input
                    type="number"
                    id="autoCashout1"
                    class="auto-input"
                    value="2.00"
                    step="0.1"
                    min="1.01"
                >

            </div>


            <div class="controls-flex">

                <div class="left-inputs">

                    <div class="input-stepper">

                        <button
                            class="step-btn"
                            onclick="adjustBet(1, -10)"
                        >
                            -
                        </button>

                        <input
                            type="number"
                            id="amount1"
                            class="amount-input"
                            value="20.00"
                            min="1"
                            step="1"
                        >

                        <button
                            class="step-btn"
                            onclick="adjustBet(1, 10)"
                        >
                            +
                        </button>

                    </div>


                    <div class="quick-bets">

                        <div
                            class="q-btn"
                            onclick="setBet(1, 100)"
                        >
                            100
                        </div>

                        <div
                            class="q-btn"
                            onclick="setBet(1, 200)"
                        >
                            200
                        </div>

                        <div
                            class="q-btn"
                            onclick="setBet(1, 500)"
                        >
                            500
                        </div>

                        <div
                            class="q-btn"
                            onclick="setBet(1, 10000)"
                        >
                            10,000
                        </div>

                    </div>

                </div>


                <button
                    class="action-btn btn-green"
                    id="btn1"
                    onclick="handleAction(1)"
                >

                    <span
                        class="btn-title"
                        id="btn1Title"
                    >
                        Bet
                    </span>

                    <span
                        class="btn-sub"
                        id="btn1Sub"
                    >
                        20.00 KES
                    </span>

                </button>

            </div>

        </div>


        <!-- PANEL 2 -->

        <div class="bet-box">

            <div class="toggle-bar">

                <div
                    class="toggle-btn active"
                    id="tabBet2"
                    onclick="switchTab(2, 'bet')"
                >
                    Bet
                </div>

                <div
                    class="toggle-btn"
                    id="tabAuto2"
                    onclick="switchTab(2, 'auto')"
                >
                    Auto
                </div>

            </div>


            <div
                class="auto-options"
                id="autoOpt2"
            >

                <label
                    style="
                        font-size:11px;
                        color:#aaa;
                    "
                >
                    Auto Cashout x
                </label>

                <input
                    type="number"
                    id="autoCashout2"
                    class="auto-input"
                    value="2.00"
                    step="0.1"
                    min="1.01"
                >

            </div>


            <div class="controls-flex">

                <div class="left-inputs">

                    <div class="input-stepper">

                        <button
                            class="step-btn"
                            onclick="adjustBet(2, -10)"
                        >
                            -
                        </button>

                        <input
                            type="number"
                            id="amount2"
                            class="amount-input"
                            value="20.00"
                            min="1"
                            step="1"
                        >

                        <button
                            class="step-btn"
                            onclick="adjustBet(2, 10)"
                        >
                            +
                        </button>

                    </div>


                    <div class="quick-bets">

                        <div
                            class="q-btn"
                            onclick="setBet(2, 100)"
                        >
                            100
                        </div>

                        <div
                            class="q-btn"
                            onclick="setBet(2, 200)"
                        >
                            200
                        </div>

                        <div
                            class="q-btn"
                            onclick="setBet(2, 500)"
                        >
                            500
                        </div>

                        <div
                            class="q-btn"
                            onclick="setBet(2, 10000)"
                        >
                            10,000
                        </div>

                    </div>

                </div>


                <button
                    class="action-btn btn-green"
                    id="btn2"
                    onclick="handleAction(2)"
                >

                    <span
                        class="btn-title"
                        id="btn2Title"
                    >
                        Bet
                    </span>

                    <span
                        class="btn-sub"
                        id="btn2Sub"
                    >
                        20.00 KES
                    </span>

                </button>

            </div>

        </div>

    </div>


    <!-- FEED -->

    <div class="feed-section">

        <div class="feed-header">

            ALL BETS
            (<span id="totalBetsCount">0</span>)

        </div>

        <div id="liveBetsList"></div>

    </div>

</div>


<script>

let currentStatus = "BETTING";

let currentMult = 1.00;

let userBets = {};

let currentRoundId = null;

let lastAutoPlacedRound = {
    1: null,
    2: null
};

let autoBetEnabled = {
    1: false,
    2: false
};

let activeTab = {
    1: "bet",
    2: "bet"
};


/* ==========================================================
   CANVAS
   ========================================================== */

const canvas = document.getElementById(
    "flightCanvas"
);

const ctx = canvas.getContext(
    "2d"
);


function resizeCanvas() {

    canvas.width =
        canvas.parentElement.clientWidth;

    canvas.height =
        canvas.parentElement.clientHeight;
}


window.addEventListener(
    "resize",
    resizeCanvas
);

resizeCanvas();


function drawScene() {

    ctx.clearRect(
        0,
        0,
        canvas.width,
        canvas.height
    );


    /* Grid */

    ctx.strokeStyle =
        "rgba(255,255,255,0.03)";

    ctx.lineWidth = 1;


    for (
        let x = 0;
        x < canvas.width;
        x += 40
    ) {

        ctx.beginPath();

        ctx.moveTo(x, 0);

        ctx.lineTo(
            x,
            canvas.height
        );

        ctx.stroke();
    }


    for (
        let y = 0;
        y < canvas.height;
        y += 40
    ) {

        ctx.beginPath();

        ctx.moveTo(0, y);

        ctx.lineTo(
            canvas.width,
            y
        );

        ctx.stroke();
    }


    if (
        currentStatus === "RUNNING"
        ||
        currentStatus === "CRASHED"
    ) {

        const progress =
            Math.min(
                (currentMult - 1) / 5.0,
                1.0
            );


        const startX = 0;

        const startY =
            canvas.height;


        const endX =
            canvas.width
            * 0.75
            * Math.min(
                progress + 0.1,
                1.0
            );


        const endY =
            canvas.height
            -
            (
                canvas.height
                * 0.65
                * Math.min(
                    progress + 0.1,
                    1.0
                )
            );


        /* Area */

        ctx.beginPath();

        ctx.moveTo(
            startX,
            startY
        );

        ctx.quadraticCurveTo(
            endX * 0.5,
            canvas.height,
            endX,
            endY
        );

        ctx.lineTo(
            endX,
            canvas.height
        );

        ctx.closePath();


        const grad =
            ctx.createLinearGradient(
                0,
                0,
                0,
                canvas.height
            );

        grad.addColorStop(
            0,
            "rgba(229,62,62,0.5)"
        );

        grad.addColorStop(
            1,
            "rgba(229,62,62,0)"
        );

        ctx.fillStyle = grad;

        ctx.fill();


        /* Line */

        ctx.beginPath();

        ctx.moveTo(
            startX,
            startY
        );

        ctx.quadraticCurveTo(
            endX * 0.5,
            canvas.height,
            endX,
            endY
        );

        ctx.strokeStyle =
            "#e53e3e";

        ctx.lineWidth = 4;

        ctx.stroke();


        /* Aircraft point */

        if (
            currentStatus === "RUNNING"
        ) {

            ctx.save();

            ctx.translate(
                endX,
                endY
            );

            ctx.fillStyle =
                "#e53e3e";

            ctx.beginPath();

            ctx.arc(
                0,
                0,
                8,
                0,
                Math.PI * 2
            );

            ctx.fill();

            ctx.restore();
        }
    }


    requestAnimationFrame(
        drawScene
    );
}


requestAnimationFrame(
    drawScene
);


/* ==========================================================
   TABS
   ========================================================== */

function switchTab(
    num,
    tab
) {

    activeTab[num] = tab;


    document.getElementById(
        "tabBet" + num
    ).className =
        "toggle-btn"
        +
        (
            tab === "bet"
                ? " active"
                : ""
        );


    document.getElementById(
        "tabAuto" + num
    ).className =
        "toggle-btn"
        +
        (
            tab === "auto"
                ? " active"
                : ""
        );


    const autoOpt =
        document.getElementById(
            "autoOpt" + num
        );


    if (tab === "auto") {
        autoOpt.classList.add(
            "show"
        );
    } else {
        autoOpt.classList.remove(
            "show"
        );
    }


    updateButtonUI(num);
}


/* ==========================================================
   STATE
   ========================================================== */

async function syncState() {

    try {

        const res =
            await fetch(
                "/api/state",
                {
                    cache: "no-store"
                }
            );


        if (!res.ok) {
            return;
        }


        const data =
            await res.json();


        document.getElementById(
            "balanceDisplay"
        ).innerText =
            Number(data.balance)
                .toFixed(2)
            + " KES";


        currentStatus =
            data.status;


        currentMult =
            Number(data.multiplier);


        userBets =
            data.bets || {};


        currentRoundId =
            data.round_id;


        /* Admin telemetry */

        if (data.is_admin) {

            document.getElementById(
                "adminBanner"
            ).style.display =
                "block";


            document.getElementById(
                "next1"
            ).innerText =
                data.next_crash_point_1
                    ? Number(
                        data.next_crash_point_1
                    ).toFixed(2) + "x"
                    : "-";


            document.getElementById(
                "next2"
            ).innerText =
                data.next_crash_point_2
                    ? Number(
                        data.next_crash_point_2
                    ).toFixed(2) + "x"
                    : "-";

        } else {

            document.getElementById(
                "adminBanner"
            ).style.display =
                "none";
        }


        /* Multiplier */

        const multDisplay =
            document.getElementById(
                "multiplierDisplay"
            );


        if (
            data.status === "BETTING"
        ) {

            multDisplay.className =
                "multiplier-overlay";

            multDisplay.innerText =
                "WAITING FOR NEXT ROUND";


            checkAndRunAutoBet(1);

            checkAndRunAutoBet(2);

        }

        else if (
            data.status === "RUNNING"
        ) {

            multDisplay.className =
                "multiplier-overlay";

            multDisplay.innerText =
                Number(
                    data.multiplier
                ).toFixed(2)
                + "x";

        }

        else if (
            data.status === "CRASHED"
        ) {

            multDisplay.className =
                "multiplier-overlay crashed-text";

            multDisplay.innerText =
                "FLEW AWAY!\n"
                +
                (
                    data.crash_point
                        ? Number(
                            data.crash_point
                        ).toFixed(2)
                        : Number(
                            data.multiplier
                        ).toFixed(2)
                )
                +
                "x";
        }


        /* History */

        const histLine =
            document.getElementById(
                "historyLine"
            );


        histLine.innerHTML =
            (data.history || [])
                .map(
                    function(val) {

                        let cls =
                            "hist-blue";

                        if (
                            val >= 10
                        ) {

                            cls =
                                "hist-pink";

                        } else if (
                            val >= 2
                        ) {

                            cls =
                                "hist-purple";
                        }


                        return `
                            <div
                                class="hist-item ${cls}"
                            >
                                ${Number(val).toFixed(2)}x
                            </div>
                        `;
                    }
                )
                .join("");


        updateButtonUI(1);

        updateButtonUI(2);


        /* Feed */

        const feedData =
            data.live_feed || [];


        document.getElementById(
            "totalBetsCount"
        ).innerText =
            feedData.length;


        const feed =
            document.getElementById(
                "liveBetsList"
            );


        feed.innerHTML =
            feedData
                .map(
                    function(b) {

                        let statusText =
                            `
                            <span
                                style="color:#888;"
                            >
                                IN PLAY
                            </span>
                            `;


                        if (
                            b.status === "WON"
                        ) {

                            const cashout =
                                Number(
                                    b.cashout_multiplier
                                    || 0
                                ).toFixed(2);


                            const winnings =
                                Number(
                                    b.winnings
                                    || 0
                                ).toFixed(2);


                            statusText =
                                `
                                <span
                                    style="
                                        color:#28a745;
                                        font-weight:bold;
                                    "
                                >
                                    ${cashout}x
                                    (+${winnings})
                                </span>
                                `;

                        }

                        else if (
                            b.status === "LOST"
                        ) {

                            statusText =
                                `
                                <span
                                    style="
                                        color:#e53e3e;
                                    "
                                >
                                    CRASHED
                                </span>
                                `;
                        }


                        return `
                            <div class="feed-row">

                                <div>
                                    <b>
                                        ${escapeHtml(
                                            b.username
                                        )}
                                    </b>
                                </div>

                                <div>
                                    ${Number(
                                        b.amount
                                    ).toFixed(2)}
                                    KES
                                </div>

                                <div>
                                    ${statusText}
                                </div>

                            </div>
                        `;
                    }
                )
                .join("");


    } catch (error) {

        console.error(
            "State sync error:",
            error
        );
    }
}


/* ==========================================================
   ESCAPE HTML
   ========================================================== */

function escapeHtml(value) {

    const div =
        document.createElement(
            "div"
        );

    div.textContent =
        String(value ?? "");

    return div.innerHTML;
}


/* ==========================================================
   AUTO BET
   ========================================================== */

async function checkAndRunAutoBet(
    num
) {

    if (
        !autoBetEnabled[num]
        ||
        currentStatus !== "BETTING"
    ) {
        return;
    }


    if (
        lastAutoPlacedRound[num]
        === currentRoundId
    ) {
        return;
    }


    const bet =
        userBets[num];


    if (
        bet
        &&
        bet.status === "ACTIVE"
    ) {

        lastAutoPlacedRound[num] =
            currentRoundId;

        return;
    }


    const amount =
        document.getElementById(
            "amount" + num
        ).value;


    const autoCash =
        document.getElementById(
            "autoCashout" + num
        ).value;


    try {

        const res =
            await fetch(
                "/api/bet",
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body: JSON.stringify({
                        bet_number: num,
                        amount: amount,
                        auto_cashout:
                            autoCash
                    })
                }
            );


        const data =
            await res.json();


        if (data.success) {

            lastAutoPlacedRound[num] =
                currentRoundId;

        }

    } catch (error) {

        console.error(
            "Auto bet error:",
            error
        );
    }
}


/* ==========================================================
   BUTTON UI
   ========================================================== */

function updateButtonUI(
    num
) {

    const btn =
        document.getElementById(
            "btn" + num
        );


    const title =
        document.getElementById(
            "btn" + num + "Title"
        );


    const sub =
        document.getElementById(
            "btn" + num + "Sub"
        );


    const inputVal =
        parseFloat(
            document.getElementById(
                "amount" + num
            ).value
        )
        || 20;


    const bet =
        userBets[num];


    if (
        currentStatus === "RUNNING"
    ) {

        if (
            bet
            &&
            bet.status === "ACTIVE"
        ) {

            const currentPayout =
                (
                    Number(bet.amount)
                    * currentMult
                ).toFixed(2);


            btn.className =
                "action-btn btn-orange";


            title.innerText =
                "CASH OUT";


            sub.innerText =
                currentPayout
                + " KES";


            btn.disabled = false;

        } else {

            btn.className =
                "action-btn btn-disabled";

            title.innerText =
                "Bet";

            sub.innerText =
                inputVal.toFixed(2)
                + " KES";

            btn.disabled = true;
        }

    }

    else if (
        currentStatus === "BETTING"
    ) {

        if (
            activeTab[num] === "auto"
        ) {

            if (
                autoBetEnabled[num]
            ) {

                btn.className =
                    "action-btn btn-blue";

                title.innerText =
                    "AUTO ACTIVE";

                sub.innerText =
                    "CANCEL";

                btn.disabled = false;

            } else {

                btn.className =
                    "action-btn btn-green";

                title.innerText =
                    "AUTO BET";

                sub.innerText =
                    inputVal.toFixed(2)
                    + " KES";

                btn.disabled = false;
            }

        } else {

            if (
                bet
                &&
                bet.status === "ACTIVE"
            ) {

                btn.className =
                    "action-btn btn-disabled";

                title.innerText =
                    "WAITING";

                sub.innerText =
                    "BET PLACED";

                btn.disabled = true;

            } else {

                btn.className =
                    "action-btn btn-green";

                title.innerText =
                    "Bet";

                sub.innerText =
                    inputVal.toFixed(2)
                    + " KES";

                btn.disabled = false;
            }
        }

    }

    else {

        btn.className =
            "action-btn btn-disabled";

        btn.disabled = true;
    }
}


/* ==========================================================
   AMOUNT CONTROLS
   ========================================================== */

function adjustBet(
    num,
    delta
) {

    const input =
        document.getElementById(
            "amount" + num
        );


    let val =
        (
            parseFloat(
                input.value
            )
            || 0
        )
        + delta;


    if (val < 1) {
        val = 1;
    }


    input.value =
        val.toFixed(2);


    updateButtonUI(num);
}


function setBet(
    num,
    val
) {

    document.getElementById(
        "amount" + num
    ).value =
        Number(val).toFixed(2);


    updateButtonUI(num);
}


/* ==========================================================
   BET / CASHOUT ACTION
   ========================================================== */

async function handleAction(
    num
) {

    const bet =
        userBets[num];


    if (
        currentStatus === "RUNNING"
        &&
        bet
        &&
        bet.status === "ACTIVE"
    ) {

        try {

            const res =
                await fetch(
                    "/api/cashout",
                    {
                        method: "POST",

                        headers: {
                            "Content-Type":
                                "application/json"
                        },

                        body: JSON.stringify({
                            bet_number: num
                        })
                    }
                );


            const data =
                await res.json();


            if (!data.success) {
                alert(
                    data.message
                );
            }

        } catch (error) {

            alert(
                "Cashout request failed."
            );
        }

    }

    else if (
        currentStatus === "BETTING"
    ) {

        if (
            activeTab[num] === "auto"
        ) {

            autoBetEnabled[num] =
                !autoBetEnabled[num];


            if (
                autoBetEnabled[num]
            ) {

                await checkAndRunAutoBet(
                    num
                );
            }

        } else {

            const amount =
                document.getElementById(
                    "amount" + num
                ).value;


            try {

                const res =
                    await fetch(
                        "/api/bet",
                        {
                            method: "POST",

                            headers: {
                                "Content-Type":
                                    "application/json"
                            },

                            body:
                                JSON.stringify({
                                    bet_number:
                                        num,

                                    amount:
                                        amount
                                })
                        }
                    );


                const data =
                    await res.json();


                if (!data.success) {

                    alert(
                        data.message
                    );
                }

            } catch (error) {

                alert(
                    "Bet request failed."
                );
            }
        }
    }


    await syncState();
}


/* ==========================================================
   NEXUS DEPOSIT
   ========================================================== */

function openDepositGateway() {

    const modal =
        document.getElementById(
            "mpesaModal"
        );


    const status =
        document.getElementById(
            "mpesaStatus"
        );


    const phoneInput =
        document.getElementById(
            "mpesaPhone"
        );


    modal.style.display =
        "flex";


    status.textContent = "";


    /*
     * We deliberately do not expose the user's phone
     * from the server into the HTML.
     *
     * They can enter the number here.
     */

    phoneInput.focus();
}


function closeMpesaModal() {

    document.getElementById(
        "mpesaModal"
    ).style.display =
        "none";
}


async function requestNexusDeposit() {

    const amount =
        parseFloat(
            document.getElementById(
                "mpesaAmount"
            ).value
        );


    const phone =
        document.getElementById(
            "mpesaPhone"
        ).value.trim();


    const status =
        document.getElementById(
            "mpesaStatus"
        );


    const btn =
        document.getElementById(
            "mpesaPayBtn"
        );


    if (
        !Number.isFinite(amount)
        ||
        amount < 1
    ) {

        status.textContent =
            "Enter a valid deposit amount.";

        return;
    }


    if (
        !Number.isInteger(amount)
    ) {

        status.textContent =
            "Enter a whole KES amount.";

        return;
    }


    if (!phone) {

        status.textContent =
            "Enter your M-Pesa phone number.";

        return;
    }


    btn.disabled = true;


    status.textContent =
        "Sending M-Pesa prompt...";


    try {

        const res =
            await fetch(
                "/api/nexus/stkpush",
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body: JSON.stringify({
                        amount: amount,
                        phone: phone
                    })
                }
            );


        const data =
            await res.json();


        if (!data.success) {

            status.textContent =
                data.message
                ||
                "Unable to start payment.";

            btn.disabled = false;

            return;
        }


        status.textContent =
            "M-Pesa prompt sent. "
            +
            "Enter your PIN on your phone. "
            +
            "Your balance will update only "
            +
            "after the payment is confirmed.";


        /*
         * Nexus Pay's publicly visible documentation
         * confirms the STK initiation endpoint but does
         * not document a confirmation endpoint here.
         *
         * Therefore we do NOT pretend the payment is
         * successful merely because STK initiation worked.
         */

        setTimeout(
            function() {
                pollNexusStatus(
                    data.reference
                );
            },
            3000
        );


    } catch (error) {

        console.error(
            "Nexus payment error:",
            error
        );


        status.textContent =
            "Payment request failed. "
            +
            "Please try again.";

    } finally {

        btn.disabled = false;
    }
}


/* ==========================================================
   NEXUS STATUS
   ========================================================== */

async function pollNexusStatus(
    reference
) {

    for (
        let i = 0;
        i < 20;
        i++
    ) {

        await new Promise(
            function(resolve) {
                setTimeout(
                    resolve,
                    3000
                );
            }
        );


        try {

            const res =
                await fetch(
                    "/api/nexus/status/"
                    +
                    encodeURIComponent(
                        reference
                    )
                );


            const data =
                await res.json();


            if (!data.success) {
                return;
            }


            if (
                data.status === "SUCCESS"
            ) {

                document.getElementById(
                    "mpesaStatus"
                ).textContent =
                    "Deposit confirmed. KES "
                    +
                    Number(
                        data.amount
                    ).toFixed(2)
                    +
                    " has been added.";


                await syncState();


                setTimeout(
                    closeMpesaModal,
                    1800
                );

                return;
            }


            if (
                data.status === "FAILED"
            ) {

                document.getElementById(
                    "mpesaStatus"
                ).textContent =
                    data.message
                    ||
                    "M-Pesa payment was not completed.";

                return;
            }

        } catch (error) {

            console.error(
                "Nexus status error:",
                error
            );

            return;
        }
    }


    document.getElementById(
        "mpesaStatus"
    ).textContent =
        "Payment is still pending. "
        +
        "If you completed the M-Pesa payment, "
        +
        "the balance will update after "
        +
        "confirmation.";
}


/* ==========================================================
   WITHDRAWAL
   ========================================================== */

async function triggerWithdrawal() {

    const val =
        prompt(
            "Enter amount to withdraw (KES):"
        );


    if (!val) {
        return;
    }


    const amount =
        parseFloat(val);


    if (
        !Number.isFinite(amount)
        ||
        amount <= 0
    ) {

        alert(
            "Invalid amount."
        );

        return;
    }


    try {

        const res =
            await fetch(
                "/api/withdraw",
                {
                    method: "POST",

                    headers: {
                        "Content-Type":
                            "application/json"
                    },

                    body: JSON.stringify({
                        amount: amount
                    })
                }
            );


        const data =
            await res.json();


        alert(
            data.message
        );


        await syncState();


    } catch (error) {

        alert(
            "Withdrawal request failed."
        );
    }
}


/* ==========================================================
   START
   ========================================================== */

setInterval(
    syncState,
    200
);

syncState();

</script>

</body>

</html>
"""


# ============================================================
# STARTUP
# ============================================================

def start_application():

    # Database must exist before the game thread starts.
    init_db()

    # Create the first game round.
    initialize_game()

    # Start game engine once.
    thread = threading.Thread(
        target=game_loop,
        daemon=True,
        name="game-engine"
    )

    thread.start()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    start_application()

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                "5000"
            )
        ),
        debug=(
            os.environ.get(
                "FLASK_DEBUG",
                "0"
            ) == "1"
        ),
        use_reloader=False
    )
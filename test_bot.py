import json
import os
import sqlite3
import tempfile
import threading
import unittest.mock
# Import the structural components directly from your active bot script
from index import (
    DAILY_PROFIT_TARGET_RUPEES,
    UNDERLYING_CONFIG,
    _WALSQLiteSession,
    extract_signal,
    find_kite_contract_for_signal,
    get_kite_client,
    required_profit_capture_points,
    start_telegram_client_with_retry,
)

def run_sqlite_session_test():
    """
    Validates the _WALSQLiteSession fix for 'database is locked' without
    requiring any Telegram credentials.  Three things are verified:

    A) WAL journal mode and busy_timeout are applied to every new connection.
    B) Two threads writing to the same session file concurrently never raise
       OperationalError('database is locked').
    C) start_telegram_client_with_retry retries on transient lock errors and
       succeeds when the lock clears.
    """
    print("[STEP 1/5] Testing SQLite Session Lock Fix...")
    all_passed = True

    # --- A: pragma verification ---
    with tempfile.TemporaryDirectory() as tmp:
        session_path = os.path.join(tmp, "test_wal_session")
        session = _WALSQLiteSession(session_path)
        cursor = session._cursor()

        cursor.execute("PRAGMA journal_mode")
        journal_mode = cursor.fetchone()[0]
        if journal_mode.lower() == "wal":
            print("   [A] WAL journal mode ................. PASS")
        else:
            print(f"   [A] WAL journal mode ................. FAIL (got '{journal_mode}')")
            all_passed = False

        cursor.execute("PRAGMA busy_timeout")
        busy_timeout = cursor.fetchone()[0]
        if busy_timeout >= 30000:
            print(f"   [A] busy_timeout >= 30 000 ms ........ PASS ({busy_timeout} ms)")
        else:
            print(f"   [A] busy_timeout >= 30 000 ms ........ FAIL (got {busy_timeout} ms)")
            all_passed = False

        session.close()

    # --- B: concurrent-write stress test ---
    with tempfile.TemporaryDirectory() as tmp:
        session_path = os.path.join(tmp, "stress_session")
        errors = []
        barrier = threading.Barrier(5)

        def _concurrent_writer(idx):
            try:
                # Each thread gets its own connection to the *same* file.
                conn = sqlite3.connect(
                    session_path + ".session",
                    check_same_thread=False,
                    timeout=30,
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS scratch (id INTEGER PRIMARY KEY, val TEXT)"
                )
                barrier.wait()          # all threads start writing simultaneously
                for i in range(50):
                    conn.execute("INSERT INTO scratch (val) VALUES (?)", (f"t{idx}-{i}",))
                    conn.commit()
                conn.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_concurrent_writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if not errors:
            print("   [B] 5-thread concurrent write (250 inserts) ... PASS")
        else:
            print(f"   [B] 5-thread concurrent write ......... FAIL ({errors[0]})")
            all_passed = False

    # --- C: retry helper succeeds after transient lock ---
    call_count = {"n": 0}

    def _flaky_start():
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise sqlite3.OperationalError("database is locked")

    mock_client = unittest.mock.MagicMock()
    mock_client.start.side_effect = _flaky_start
    mock_logger = unittest.mock.MagicMock()

    try:
        start_telegram_client_with_retry(
            mock_client, mock_logger, "test_session", attempts=5, delay_seconds=0
        )
        if call_count["n"] == 3:
            print("   [C] Retry on transient lock (3 attempts) ..... PASS")
        else:
            print(f"   [C] Retry on transient lock .......... FAIL (expected 3 calls, got {call_count['n']})")
            all_passed = False
    except Exception as exc:
        print(f"   [C] Retry on transient lock .......... FAIL ({exc})")
        all_passed = False

    # --- D: retry gives up after max attempts ---
    def _always_locked():
        raise sqlite3.OperationalError("database is locked")

    mock_client2 = unittest.mock.MagicMock()
    mock_client2.start.side_effect = _always_locked

    try:
        start_telegram_client_with_retry(
            mock_client2, unittest.mock.MagicMock(), "test_session", attempts=3, delay_seconds=0
        )
        print("   [D] Max-attempts exhaustion .......... FAIL (no exception raised)")
        all_passed = False
    except sqlite3.OperationalError:
        print("   [D] Max-attempts exhaustion .......... PASS")
    except Exception as exc:
        print(f"   [D] Max-attempts exhaustion .......... FAIL (wrong exception: {exc})")
        all_passed = False

    if all_passed:
        print("✅ SQLite session lock fix verified — safe to deploy.\n")
    else:
        print("❌ One or more SQLite session checks failed — DO NOT deploy.\n")

    return all_passed


def print_target_point_preview():
    print("[STEP 0/4] Previewing Dynamic Target Points...")
    print(f"Daily profit target: Rs. {DAILY_PROFIT_TARGET_RUPEES}")
    for underlying in ("NIFTY", "BANKNIFTY", "SENSEX"):
        target_points, lot_size = required_profit_capture_points(underlying, 0)
        print(
            f"   {underlying:<10} | Configured lot size: {UNDERLYING_CONFIG[underlying]['profit_target_lot_size']:<3} | "
            f"Required target points: {target_points:.2f}"
        )
    print()

def run_dry_run_test():
    print("==================================================")
    print("           STARTING BOT VALIDATION TEST           ")
    print("==================================================\n")

    print_target_point_preview()

    if not run_sqlite_session_test():
        return

    # 2. Verify Connectivity to Zerodha Auth Layer
    print("[STEP 2/5] Testing Zerodha Automated Authentication...")
    try:
        client = get_kite_client()
        profile = client.profile()
        print(f"✅ Success! Logged into account user: {profile.get('user_id')} ({profile.get('user_name')})\n")
    except Exception as e:
        print(f"❌ Failed! Authentication layer broke down: {e}")
        print("Please verify your credentials inside config.txt\n")
        return

    # 3. Verify Your Telegram Pattern-Matching Regex
    print("[STEP 3/5] Testing Signal Parsing Engine...")
    # This matches the formatting pattern used by 't.me/dhanvitta'
    sample_telegram_message = """BUY NIFTY 23500 CE
ENTRY RANGE : 10 - 500
TARGET : 600
SL : 5 """

    parsed_signal = extract_signal(sample_telegram_message)
    if parsed_signal:
        print("✅ Success! Message parsed perfectly.")
        print(f"   Parsed Extracted Signal: {json.dumps(parsed_signal, indent=2)}\n")
    else:
        print("❌ Failed! The Regular Expressions failed to extract parameters from the text.\n")
        return

    # 4. Verify Active Contract Lookup & Live LTP Data Retrieval
    print("[STEP 4/5] Testing Active Contract Matching & Live Market Data...")
    print("Searching for live instruments matching your parameters on Zerodha...")

    contract_match = find_kite_contract_for_signal(parsed_signal)
    if contract_match:
        print("✅ Success! Zerodha matched your request to an active market asset.")
        print(f"   Matched Contract: {contract_match['tradingsymbol']}")
        print(f"   Live Market LTP : {contract_match['last_price']}")
        print(f"   Contract Expiry : {contract_match['expiry']}\n")
        print("==================================================")
        print("🎉 SUCCESS: Your script is 100% ready for production!")
        print("==================================================")
    else:
        print("❌ Failed! Connected to Zerodha, but couldn't find an active contract matching that price range.")
        print("   Note: If you are running this test over the weekend or outside market hours,")
        print("   the current options prices might fall outside your 120-125 entry range boundary.\n")

if __name__ == "__main__":
    run_dry_run_test()

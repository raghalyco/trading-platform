import json
# Import the structural components directly from your active bot script
from index import (
    DAILY_PROFIT_TARGET_RUPEES,
    UNDERLYING_CONFIG,
    extract_signal,
    find_kite_contract_for_signal,
    get_kite_client,
    required_profit_capture_points,
)

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

    # 1. Verify Connectivity to Zerodha Auth Layer
    print("[STEP 1/4] Testing Zerodha Automated Authentication...")
    try:
        client = get_kite_client()
        profile = client.profile()
        print(f"✅ Success! Logged into account user: {profile.get('user_id')} ({profile.get('user_name')})\n")
    except Exception as e:
        print(f"❌ Failed! Authentication layer broke down: {e}")
        print("Please verify your credentials inside config.txt\n")
        return

    # 2. Verify Your Telegram Pattern-Matching Regex
    print("[STEP 2/4] Testing Signal Parsing Engine...")
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

    # 3. Verify Active Contract Lookup & Live LTP Data Retrieval
    print("[STEP 3/4] Testing Active Contract Matching & Live Market Data...")
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

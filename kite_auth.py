import os
import configparser
import requests
import pyotp
from kiteconnect import KiteConnect

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.txt")
TOKEN_PATH = os.path.join(BASE_DIR, "access_token.txt")

def generate_fresh_token():
    """Simulates a secure browser login session to extract tokens automatically via JSON payloads"""
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    c = config["ZERODHA"]
    
    # Defensive cleanup: safely strip out quotes or spacing wrapping raw configs
    user_id = c["user_id"].strip().strip('"')
    password = c["password"].strip().strip('"')
    totp_secret = c["totp_secret"].replace(" ", "").strip().strip('"')
    api_key = c["api_key"].strip().strip('"')
    api_secret = c["api_secret"].strip().strip('"')
    
    kite = KiteConnect(api_key=api_key)
    session = requests.Session()
    
    # Core browser simulation headers to safely clear CSRF defensive filters
    browser_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://kite.zerodha.com/",
        "X-Kite-Version": "3"
    }
    session.headers.update(browser_headers)
    
    # Step 1: Establish base connection cookie jar
    login_url = f"https://kite.trade/connect/login?v=3&api_key={api_key}"
    session.get(login_url)
    
    # Step 2: Submit username and password securely via pure JSON
    login_payload = {"user_id": user_id, "password": password}
    login_response = session.post("https://kite.zerodha.com/api/login", json=login_payload)
    login_res = login_response.json()
    
    if login_res.get("status") == "error":
        raise Exception(f"Zerodha Login Rejected: {login_res.get('message')} (Check User ID or Password)")
        
    request_id = login_res["data"]["request_id"]
    
    # Step 3: Generate TOTP token algorithmically and complete 2FA via JSON
    totp_token = pyotp.TOTP(totp_secret).now()
    twofa_payload = {
        "user_id": user_id,
        "request_id": request_id,
        "twofa_value": totp_token,
        "twofa_type": "totp",
        "skip_session": "true"
    }
    twofa_response = session.post("https://kite.zerodha.com/api/twofa", json=twofa_payload)
    twofa_res = twofa_response.json()
    
    if twofa_res.get("status") == "error":
        raise Exception(f"Zerodha 2FA Rejected: {twofa_res.get('message')} (Check your 16-digit master TOTP Key)")
    
    # Step 4: Extract token from the final browser redirection path
    final_res = session.get(login_url, allow_redirects=True)
    if "request_token=" not in final_res.url:
        raise Exception(f"Redirection Failure. Flow landed on: {final_res.url}")
        
    request_token = final_res.url.split("request_token=")[1].split("&")[0]
    
    # Step 5: Convert request token to a structural Kite Access Token
    auth_data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = auth_data["access_token"]
    
    with open(TOKEN_PATH, "w") as f:
        f.write(access_token)
        
    return access_token

def get_kite_client():
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    c = config["ZERODHA"]
    api_key = c["api_key"].strip().strip('"')
    
    kite = KiteConnect(api_key=api_key)
    
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "r") as f:
            token = f.read().strip()
        kite.set_access_token(token)
        try:
            kite.profile()
            return kite
        except Exception:
            pass
            
    new_token = generate_fresh_token()
    kite.set_access_token(new_token)
    return kite

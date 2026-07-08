import os
import configparser
import sys
from kiteconnect import KiteConnect

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.txt")
TOKEN_PATH = os.path.join(BASE_DIR, "access_token.txt")

def load_zerodha_config():
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    c = config["ZERODHA"]
    return {
        "api_key": c["api_key"].strip().strip('"'),
        "api_secret": c["api_secret"].strip().strip('"'),
    }

def get_manual_login_url():
    settings = load_zerodha_config()
    return f"https://kite.trade/connect/login?v=3&api_key={settings['api_key']}"

def exchange_request_token(request_token):
    settings = load_zerodha_config()
    request_token = request_token.strip()
    if not request_token:
        raise ValueError("Request token is empty.")

    if "request_token=" in request_token:
        request_token = request_token.split("request_token=", 1)[1].split("&", 1)[0]

    if len(request_token) < 10:
        raise ValueError("Request token looks too short.")

    kite = KiteConnect(api_key=settings["api_key"])
    auth_data = kite.generate_session(request_token, api_secret=settings["api_secret"])
    access_token = auth_data["access_token"]

    with open(TOKEN_PATH, "w") as f:
        f.write(access_token)

    return access_token

def prompt_and_store_access_token(request_token=None):
    if request_token is None:
        print("Open this URL in a browser, complete the Zerodha login, then paste the request_token or redirect URL here:")
        print(get_manual_login_url())
        request_token = input("Request token / redirect URL: ").strip()

    exchange_request_token(request_token)
    print("Access token updated successfully.")

def generate_fresh_token():
    login_url = get_manual_login_url()
    raise RuntimeError(
        "Automatic Zerodha username/password login is disabled. "
        "Generate a fresh request_token manually and run `python kite_auth.py`, "
        f"or paste the token/redirect URL into `prompt_and_store_access_token()`. Login URL: {login_url}"
    )

def get_kite_client():
    settings = load_zerodha_config()
    api_key = settings["api_key"]
    kite = KiteConnect(api_key=api_key)

    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "r") as f:
            token = f.read().strip()
        kite.set_access_token(token)
        try:
            kite.profile()
            return kite
        except Exception as exc:
            raise RuntimeError(
                "Saved Zerodha access token is missing or expired. "
                "Refresh it manually by running `python kite_auth.py` and paste the request_token or redirect URL. "
                f"Login URL: {get_manual_login_url()}"
            ) from exc

    raise RuntimeError(
        "Zerodha access token file not found. "
        "Run `python kite_auth.py`, complete the browser login, and paste the request_token or redirect URL. "
        f"Login URL: {get_manual_login_url()}"
    )

if __name__ == "__main__":
    request_token = sys.argv[1] if len(sys.argv) > 1 else None
    prompt_and_store_access_token(request_token)

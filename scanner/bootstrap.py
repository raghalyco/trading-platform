"""
Makes `python app.py` alone sufficient to get running — no separate
`pip install`, no manual `copy .env.example .env`, no manual mkdir. Uses
ONLY the standard library (must run before we even know if third-party
packages are installed).

What it can't automate: your actual Kite Connect API key/secret — that's a
private credential only you have, so the first run will still stop and ask
you to fill in `.env`. Everything else is automatic.
"""
import importlib
import os
import subprocess
import sys

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# module import name -> pip package name (only differ for python-dotenv/dateutil)
_REQUIRED_MODULES = {
    "flask": "flask",
    "pandas": "pandas",
    "numpy": "numpy",
    "pyarrow": "pyarrow",
    "kiteconnect": "kiteconnect",
    "dotenv": "python-dotenv",
    "requests": "requests",
    "tqdm": "tqdm",
    "yfinance": "yfinance",
    "dateutil": "python-dateutil",
}


def _missing_packages():
    missing = []
    for module_name, pip_name in _REQUIRED_MODULES.items():
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing.append(pip_name)
    return missing


def ensure_directories():
    for d in ("cache", "data", "results"):
        os.makedirs(os.path.join(_BASE_DIR, d), exist_ok=True)


def ensure_env_file():
    env_path = os.path.join(_BASE_DIR, ".env")
    example_path = os.path.join(_BASE_DIR, ".env.example")
    if not os.path.exists(env_path) and os.path.exists(example_path):
        with open(example_path) as f:
            content = f.read()
        with open(env_path, "w") as f:
            f.write(content)
        print(f"Created {env_path} from .env.example (first run).")
        print("Fill in your real Kite Connect api_key/api_secret (and optionally "
              "Telegram bot token) in that file, then run `python app.py` again.")


def ensure_dependencies():
    missing = _missing_packages()
    if not missing:
        return

    print(f"Missing dependencies detected: {', '.join(missing)}")
    print("Installing automatically from requirements.txt...")
    req_path = os.path.join(_BASE_DIR, "requirements.txt")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req_path])
    except subprocess.CalledProcessError as e:
        print(f"Automatic install failed ({e}).")
        print(f"Please run manually: pip install -r requirements.txt")
        sys.exit(1)

    print("\nDependencies installed. Please run `python app.py` again to start "
          "(this two-step happens only once, on a fresh setup).")
    sys.exit(0)


def bootstrap():
    ensure_directories()
    ensure_env_file()
    ensure_dependencies()

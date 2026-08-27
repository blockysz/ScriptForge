import os
import sys
import json
import re
import time
import hashlib
import secrets
import urllib.request
import urllib.parse
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

app = Flask(__name__)
CORS(app)

# Persistent Local & Cloud Storage Setup
ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), "accounts.json")
KV_URL = os.getenv("KV_REST_API_URL") or os.getenv("UPSTASH_REDIS_REST_URL")
KV_TOKEN = os.getenv("KV_REST_API_TOKEN") or os.getenv("UPSTASH_REDIS_REST_TOKEN")

accounts_in_memory = {}

def load_accounts():
    """Load user accounts safely from Vercel KV / Upstash Redis or local JSON database."""
    global accounts_in_memory

    if KV_URL and KV_TOKEN:
        try:
            url = f"{KV_URL.rstrip('/')}/get/scriptforge_accounts"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {KV_TOKEN}"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                val = res.get("result")
                if val:
                    accounts_in_memory = json.loads(val)
                    return accounts_in_memory
        except Exception as e:
            print(f"[ACCOUNTS] Error reading Vercel KV: {e}")

    if accounts_in_memory:
        return accounts_in_memory

    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                accounts_in_memory = data
                return data
        except Exception as e:
            print(f"[ACCOUNTS] Error loading accounts file: {e}")

    return accounts_in_memory

def save_accounts(accounts):
    """Save user accounts safely to Vercel KV / Upstash Redis and local JSON database."""
    global accounts_in_memory
    accounts_in_memory = accounts

    if KV_URL and KV_TOKEN:
        try:
            url = f"{KV_URL.rstrip('/')}/set/scriptforge_accounts"
            payload = json.dumps(accounts)
            req = urllib.request.Request(
                url,
                data=payload.encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {KV_TOKEN}",
                    "Content-Type": "application/json"
                }
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                print(f"[ACCOUNTS] Saved accounts to Vercel KV!")
        except Exception as e:
            print(f"[ACCOUNTS] Error saving to Vercel KV: {e}")

    try:
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
    except OSError as e:
        print(f"[ACCOUNTS] Read-only filesystem warning: {e}")
    except Exception as e:
        print(f"[ACCOUNTS] Error saving local accounts file: {e}")

# Global Multi-Tenant State
sessions_data = {}
game_name_cache = {}

openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")

@app.errorhandler(Exception)
def handle_exception(e):
    """Ensure all server errors return clean JSON instead of HTML pages."""
    return jsonify({"error": str(e)}), 500

def hash_password(password, salt=None):
    """Safely hash password using PBKDF2 HMAC SHA-256 with 100,000 iterations."""
    if not salt:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
    return key.hex(), salt

def verify_password(password, stored_hash, salt):
    """Verify password against stored PBKDF2 hash."""
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
    return key.hex() == stored_hash

def get_session_key(req):
    """Extract secure session_key from header, query param, or JSON body."""
    key = req.headers.get("X-Session-Key")
    if not key:
        key = req.args.get("session_key")
    if not key and req.is_json:
        data = req.get_json(silent=True) or {}
        key = data.get("session_key")
    return key or "default_session"

def get_session_store(key):
    """Get or initialize isolated session data for a given session_key."""
    if key not in sessions_data:
        sessions_data[key] = {
            "game_context": {
                "connected": False,
                "last_seen": None,
                "place_id": 0,
                "player_name": "Disconnected",
                "remotes": [],
                "leaderstats": {},
                "workspace_items": []
            },
            "pending_scripts": [],
            "script_sessions": {}
        }
    return sessions_data[key]

def fetch_game_name(place_id):
    """Fetch official Roblox game name by place ID with caching."""
    if not place_id or str(place_id) == "0":
        return "General Roblox"
    place_key = str(place_id)
    if place_key in game_name_cache:
        return game_name_cache[place_key]
    
    url = f"https://economy.roblox.com/v2/assets/{place_id}/details"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            name = data.get("Name", f"Place {place_id}")
            game_name_cache[place_key] = name
            return name
    except Exception as e:
        return f"Place {place_id}"

def extract_luau_code(text):
    """Extract clean Luau code from AI response with foolproof parsing."""
    if not text:
        return ""
    blocks = re.findall(r"```(?:luau|lua)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if blocks:
        return blocks[0].strip()
    
    lines = text.splitlines()
    code_lines = [l for l in lines if not l.strip().lower().startswith(('here', 'sure', 'i have', 'certainly', 'this script', 'below is', 'note:'))]
    cleaned = "\n".join(code_lines).strip()
    return cleaned

def call_openai_compatible(api_url, api_key, model_name, system_instruction, user_prompt, history=[], game_ctx={}):
    """Call OpenRouter API endpoints."""
    if not api_key:
        return None, "OpenRouter API Key is missing. Please add your key in Settings."

    messages = [{"role": "system", "content": f"{system_instruction}\n\n[LIVE ROBLOX GAME CONTEXT]\n{json.dumps(game_ctx, indent=2)}"}]
    for item in history:
        r = item.get("role", "user")
        role = "assistant" if r in ["ai", "assistant", "model"] else "user"
        messages.append({"role": role, "content": item.get("content", "")})
    messages.append({"role": "user", "content": user_prompt})

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.7
    }

    req = urllib.request.Request(api_url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return content, None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        return None, f"HTTP {e.code}: {err_body}"
    except Exception as e:
        return None, str(e)

def generate_ai_response(user_prompt, selected_model, history=[], game_ctx={}, openrouter_key="", ai_mode="coding"):
    """UNIFIED AI generation engine powering all models strictly through OpenRouter."""
    
    if ai_mode == "thinking":
        system_instruction = """
        You are ScriptForge's Deep Reasoning & Architecture Assistant connected directly to a live Roblox game player session.
        Your goal:
        1. Perform deep reasoning, step-by-step planning, and architectural breakdown for Roblox game systems (RemoteEvents, DataStores, Inventory systems).
        2. Provide thorough technical analysis and explanation of game logic. Do not write code intended for execution.
        """
    elif ai_mode == "chat":
        system_instruction = """
        You are ScriptForge's friendly AI Chat Assistant connected directly to a live Roblox game player session.
        Your goal:
        1. Answer game design, scripting, and Roblox concept questions conversationally in standard Markdown.
        2. Provide helpful explanations, tips, and guidelines without writing automated test code.
        """
    else: # Default: Coding Mode
        system_instruction = """
        You are ScriptForge's expert Luau Scripting Assistant connected directly to a live Roblox game player session.
        Your primary goal is writing, testing, auto-fixing, and optimizing valid Luau code inside ```luau ... ``` blocks suitable for execution.
        Use exact Remote names, leaderstats, and workspace paths from live context.
        """

    m_name = selected_model.replace("openrouter/", "")
    key = openrouter_key or os.getenv("OPENROUTER_API_KEY", "")
    print(f"[AI PIPELINE] Generating response via OpenRouter: {m_name} (Mode: {ai_mode})")

    return call_openai_compatible("https://openrouter.ai/api/v1/chat/completions", key, m_name, system_instruction, user_prompt, history, game_ctx)

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ScriptForge | Live Roblox AI Studio</title>
    
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='20' fill='%230a0a0a'/><path d='M25 65 L75 65 L70 50 L30 50 Z M40 50 L40 35 L60 35 L60 50 Z' fill='%23ffffff'/><polygon points='52 20 38 48 50 48 44 75 62 42 50 42' fill='%23ffffff'/></svg>">
    
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.7.0/styles/github-dark.min.css" id="highlightTheme">
    <style>
        :root {
            --bg-main: #0a0a0a;
            --bg-sidebar: #111111;
            --bg-card: #181818;
            --border-color: #262626;
            --text-primary: #f0f0f0;
            --text-secondary: #888888;
            --accent: #ffffff;
            --accent-hover: #e0e0e0;
            --btn-text: #000000;
            --input-bg: #141414;
        }

        [data-bs-theme="light"] {
            --bg-main: #ffffff;
            --bg-sidebar: #f5f5f5;
            --bg-card: #e8e8e8;
            --border-color: #d0d0d0;
            --text-primary: #121212;
            --text-secondary: #555555;
            --accent: #000000;
            --accent-hover: #333333;
            --btn-text: #ffffff;
            --input-bg: #f0f0f0;
        }

        html, body {
            background-color: var(--bg-main);
            color: var(--text-primary);
            font-family: Söhne, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            height: 100vh;
            width: 100vw;
            display: flex;
            flex-direction: column;
            margin: 0;
            padding: 0;
            overflow: hidden !important;
        }

        .toast-popup {
            position: fixed;
            top: -80px;
            left: 50%;
            transform: translateX(-50%);
            background-color: var(--bg-sidebar);
            color: var(--text-primary);
            border: 1px solid var(--border-color);
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.7);
            padding: 12px 24px;
            border-radius: 30px;
            font-size: 0.88rem;
            font-weight: 600;
            z-index: 99999;
            display: flex;
            align-items: center;
            gap: 10px;
            transition: all 0.35s cubic-bezier(0.68, -0.55, 0.265, 1.55);
            opacity: 0;
        }

        .toast-popup.show {
            top: 20px;
            opacity: 1;
        }

        .header {
            background-color: var(--bg-sidebar);
            border-bottom: 1px solid var(--border-color);
            padding: 10px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-shrink: 0;
        }

        .header-title {
            font-weight: 700;
            font-size: 1.05rem;
            display: flex;
            align-items: center;
            gap: 8px;
            letter-spacing: 0.5px;
        }

        .status-badge {
            padding: 4px 12px;
            border-radius: 16px;
            font-size: 0.78rem;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            border: 1px solid var(--border-color);
        }

        .status-online {
            background-color: rgba(255, 255, 255, 0.08);
            color: var(--text-primary);
        }

        .status-offline {
            background-color: rgba(100, 100, 100, 0.08);
            color: var(--text-secondary);
        }

        .dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
        }
        .dot-online { background-color: #ffffff; box-shadow: 0 0 6px #ffffff; }
        .dot-offline { background-color: #666666; }

        .main-container {
            display: flex;
            flex: 1;
            width: 100%;
            height: calc(100vh - 53px);
            overflow: hidden;
        }

        .sidebar {
            width: 250px !important;
            min-width: 250px !important;
            max-width: 250px !important;
            flex: 0 0 250px !important;
            background-color: var(--bg-sidebar);
            border-right: 1px solid var(--border-color);
            display: flex;
            flex-direction: column;
            overflow: hidden;
            height: 100%;
        }

        .sidebar-section {
            padding: 12px;
            border-bottom: 1px solid var(--border-color);
        }

        .chat-list {
            flex: 1;
            overflow-y: auto;
            overflow-x: hidden;
            padding: 8px;
            display: flex;
            flex-direction: column;
            gap: 3px;
        }

        .section-divider {
            font-size: 0.7rem;
            font-weight: 700;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin: 10px 4px 4px 4px;
        }

        .chat-item {
            padding: 8px 10px;
            border-radius: 8px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: space-between;
            color: var(--text-secondary);
            font-size: 0.85rem;
            transition: all 0.15s;
        }

        .chat-item:hover, .chat-item.active {
            background-color: var(--bg-card);
            color: var(--text-primary);
        }

        .chat-item.active {
            font-weight: 600;
        }

        .chat-item-title-box {
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            flex: 1;
            display: flex;
            flex-direction: column;
        }

        .chat-item-title {
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .inline-rename-input {
            background: transparent !important;
            border: none !important;
            outline: none !important;
            box-shadow: none !important;
            color: var(--text-primary);
            font-size: 0.85rem;
            font-weight: 600;
            font-family: inherit;
            padding: 0;
            margin: 0;
            width: 100%;
            box-sizing: border-box;
        }

        .chat-item-sub {
            font-size: 0.72rem;
            color: var(--text-secondary);
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            opacity: 0.8;
        }

        .chat-item-actions {
            opacity: 0;
            transition: opacity 0.2s;
            display: flex;
            gap: 6px;
        }

        .chat-item:hover .chat-item-actions {
            opacity: 1;
        }

        .chat-action-btn {
            color: var(--text-secondary);
            padding: 2px 4px;
            cursor: pointer;
            transition: color 0.15s;
        }

        .chat-action-btn:hover {
            color: var(--text-primary);
        }

        .chat-area {
            flex: 1;
            display: flex;
            flex-direction: column;
            background-color: var(--bg-main);
            min-width: 0;
            height: 100%;
            overflow: hidden !important;
            position: relative;
        }

        .chat-messages {
            flex: 1;
            padding: 24px;
            overflow-y: auto;
            overflow-x: hidden;
            display: flex;
            flex-direction: column;
            gap: 20px;
            width: 100% !important;
            max-width: 100% !important;
            box-sizing: border-box;
        }

        .message-row {
            display: flex;
            gap: 14px;
            width: 100%;
            box-sizing: border-box;
        }

        .avatar {
            width: 32px;
            height: 32px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.85rem;
            flex-shrink: 0;
            border: 1px solid var(--border-color);
        }
        .avatar-user { background-color: var(--bg-card); color: var(--text-primary); }
        .avatar-ai { background-color: var(--accent); color: var(--btn-text); }

        .message-content {
            flex: 1;
            line-height: 1.6;
            font-size: 0.95rem;
            word-wrap: break-word;
            overflow-wrap: break-word;
            padding-top: 4px;
            min-width: 0;
        }

        .agent-trajectory-card {
            background-color: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 12px 16px;
            margin-top: 8px;
            font-size: 0.84rem;
        }

        .agent-trajectory-header {
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 8px;
            color: var(--text-primary);
        }

        .agent-step {
            display: flex;
            align-items: flex-start;
            gap: 8px;
            margin-bottom: 4px;
            color: var(--text-secondary);
            font-family: monospace;
        }

        .input-container {
            width: 100% !important;
            max-width: 100% !important;
            padding: 0 24px 16px 24px;
            box-sizing: border-box;
            overflow: visible !important;
            flex-shrink: 0;
            display: flex;
            flex-direction: column;
            gap: 10px;
            position: relative;
            z-index: 10;
        }

        .prompt-pills {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            overflow: hidden;
        }

        .pill-btn {
            background-color: var(--bg-card);
            color: var(--text-secondary);
            border: 1px solid var(--border-color);
            padding: 4px 12px;
            border-radius: 16px;
            font-size: 0.78rem;
            cursor: pointer;
            white-space: nowrap;
            transition: all 0.2s;
        }
        .pill-btn:hover {
            color: var(--text-primary);
            border-color: var(--text-primary);
        }

        .input-box {
            background-color: var(--input-bg);
            border: 1px solid var(--border-color);
            border-radius: 24px;
            padding: 10px 16px;
            display: flex;
            align-items: center;
            gap: 10px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
            transition: border-color 0.2s;
            width: 100%;
            box-sizing: border-box;
        }

        .input-box:focus-within {
            border-color: var(--text-primary);
        }

        /* Fix Textarea to eliminate scrollbar arrows completely */
        .input-box textarea {
            flex: 1;
            background: transparent;
            border: none;
            color: var(--text-primary);
            resize: none !important;
            height: 24px;
            max-height: 120px;
            outline: none;
            font-size: 0.95rem;
            line-height: 1.4;
            overflow-y: hidden !important;
            overflow-x: hidden !important;
        }

        .send-btn {
            background-color: var(--accent);
            color: var(--btn-text);
            border: none;
            width: 32px;
            height: 32px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: background-color 0.2s;
            flex-shrink: 0;
        }

        .send-btn:hover {
            background-color: var(--accent-hover);
        }

        .bottom-toggles-bar {
            background-color: var(--bg-sidebar);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 6px 14px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            position: relative;
            z-index: 1000;
        }

        .toggle-group {
            display: flex;
            align-items: center;
            gap: 12px;
            position: relative;
        }

        .custom-toggle-pill {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background-color: #161616;
            border: 1px solid #282828;
            padding: 5px 12px;
            border-radius: 20px;
            cursor: pointer;
            font-size: 0.8rem;
            font-weight: 600;
            color: #777777;
            transition: all 0.2s ease-in-out;
            user-select: none;
        }

        .custom-toggle-pill:hover {
            border-color: #444444;
            color: #bbbbbb;
        }

        .custom-toggle-pill.active {
            background-color: #222222;
            border-color: #444444;
            color: #ffffff;
        }

        .toggle-knob {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background-color: #444444;
            transition: all 0.2s ease-in-out;
            display: inline-block;
        }

        .custom-toggle-pill.active .toggle-knob {
            background-color: #ffffff;
            box-shadow: 0 0 5px rgba(255, 255, 255, 0.5);
        }

        .dropup {
            position: relative !important;
        }

        .dropup .dropdown-menu {
            z-index: 99999 !important;
            position: absolute !important;
            bottom: 100% !important;
            top: auto !important;
            left: 0 !important;
            transform: none !important;
            margin-bottom: 3px !important;
            max-height: 360px;
            overflow-y: auto;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.9);
            border: 1px solid var(--border-color);
            background-color: #121212 !important;
        }

        /* Hardware Accelerated Smooth Dropup Fade & Slide Animation */
        #modeInputDropup .dropdown-menu {
            display: block !important;
            visibility: hidden;
            opacity: 0;
            transform: translateY(12px) scale(0.94) !important;
            transition: opacity 0.22s cubic-bezier(0.16, 1, 0.3, 1), transform 0.22s cubic-bezier(0.16, 1, 0.3, 1), visibility 0.22s !important;
            pointer-events: none;
            z-index: 99999 !important;
            position: absolute !important;
            bottom: 100% !important;
            top: auto !important;
            right: 0 !important;
            left: auto !important;
            margin-bottom: 6px !important;
            user-select: none;
            -webkit-user-select: none;
        }

        #modeInputDropup .dropdown-menu.show {
            visibility: visible !important;
            opacity: 1 !important;
            transform: translateY(0) scale(1) !important;
            pointer-events: auto !important;
        }

        .code-container {
            margin: 12px 0;
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid var(--border-color);
            max-width: 100%;
        }

        .code-header {
            background: #141414;
            padding: 6px 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.78rem;
            color: #888888;
            border-bottom: 1px solid #222222;
        }

        pre {
            margin: 0;
            padding: 14px;
            background: #080808 !important;
            overflow-x: auto;
            max-width: 100%;
        }
    </style>
</head>
<body>

    <div id="toastPopup" class="toast-popup">
        <i class="fa-solid fa-circle-check text-light" id="toastIcon"></i>
        <span id="toastMsg">Notification</span>
    </div>

    <div class="header">
        <div class="header-title">
            <i class="fa-solid fa-hammer text-light me-1"></i> SCRIPTFORGE
        </div>

        <div class="d-flex align-items-center gap-2">
            <button class="btn btn-sm btn-outline-light fw-bold" onclick="openAuthModal()" id="authHeaderBtn">
                <i class="fa-solid fa-user me-1"></i> Login / Sign Up
            </button>

            <button class="btn btn-sm btn-outline-light fw-bold" onclick="openExecutorModal()" title="Get Executor Client Script">
                <i class="fa-solid fa-plug me-1"></i> Connect Executor
            </button>

            <div id="statusBadge" class="status-badge status-offline">
                <span class="dot dot-offline"></span> Disconnected
            </div>

            <button class="btn btn-sm btn-outline-secondary border-0 text-light" onclick="toggleTheme()" id="themeBtn" title="Toggle Light / Dark Mode">
                <i class="fa-solid fa-moon"></i>
            </button>

            <button class="btn btn-sm btn-outline-secondary border-0 text-light" onclick="openSettingsModal()" title="API Keys & Settings">
                <i class="fa-solid fa-gear"></i>
            </button>
        </div>
    </div>

    <div class="main-container">
        <div class="sidebar">
            <div class="sidebar-section">
                <button class="btn btn-sm btn-outline-light w-100 fw-bold py-1 text-start ps-3 mb-2" onclick="createNewChat()">
                    <i class="fa-solid fa-plus me-2"></i> New chat
                </button>

                <!-- Form wrapper with readonly anti-autofill trick to permanently prevent Chrome autofill -->
                <form autocomplete="off" action="javascript:void(0);" class="m-0 p-0">
                    <div class="input-group input-group-sm mb-2">
                        <span class="input-group-text bg-black text-secondary border-secondary"><i class="fa-solid fa-magnifying-glass"></i></span>
                        <input type="search" id="chatSearchInput" name="sf_search_q_noautofill" autocomplete="chrome-off" readonly onfocus="this.removeAttribute('readonly');" class="form-control bg-black text-light border-secondary" placeholder="Search chats & messages..." oninput="renderChatList()">
                    </div>
                </form>

                <select id="gameFilterSelect" class="form-select form-select-sm bg-black text-light border-secondary" onchange="renderChatList()">
                    <option value="all">🎮 All Games</option>
                </select>
            </div>

            <div class="chat-list" id="chatList"></div>

            <div class="sidebar-section bg-black">
                <div class="text-secondary mb-2 fw-bold" style="font-size: 0.7rem;"><i class="fa-solid fa-gamepad me-1"></i> GAME SESSION</div>
                <div class="d-flex flex-column gap-1" style="font-size: 0.78rem;">
                    <div class="d-flex justify-content-between"><span class="text-secondary">Player:</span> <span class="fw-bold text-light" id="statPlayer">-</span></div>
                    <div class="d-flex justify-content-between"><span class="text-secondary">Game:</span> <span class="fw-bold text-light text-truncate" id="statGame" style="max-width: 120px;">-</span></div>
                    <div class="d-flex justify-content-between"><span class="text-secondary">Remotes:</span> <span class="fw-bold text-light" id="statRemotes">0</span></div>
                </div>
                <button class="btn btn-sm btn-outline-secondary text-light w-100 mt-2 fw-bold" style="font-size: 0.75rem;" onclick="openExecutorModal()">
                    <i class="fa-solid fa-code me-1"></i> Get Client Script
                </button>
            </div>
        </div>

        <div class="chat-area">
            <div class="chat-messages" id="chatMessages"></div>

            <div class="input-container">
                <div class="prompt-pills">
                    <button class="pill-btn" onclick="sendQuickPrompt('Fire the money remote to add cash')">⚡ Auto-Farm Remotes</button>
                    <button class="pill-btn" onclick="sendQuickPrompt('Set walkspeed to 100 and jump power to 150')">⚡ Speed & Jump Hack</button>
                    <button class="pill-btn" onclick="sendQuickPrompt('Teleport me to closest coin or chest model')">🚀 Teleport to Items</button>
                    <button class="pill-btn" onclick="sendQuickPrompt('List all ReplicatedStorage Remotes and Workspace items')">🔍 Inspect Workspace</button>
                </div>

                <div class="input-box">
                    <textarea id="userInput" placeholder="Ask ScriptForge to write a Luau script for your game..." oninput="autoGrow(this)" onkeydown="handleKeyDown(event)"></textarea>

                    <!-- Animated & Larger Mode Selector Dropup Button with onmousedown preventDefault to prevent text selection -->
                    <div class="dropup d-inline-block me-2 align-self-center" id="modeInputDropup" style="position: relative; user-select: none; -webkit-user-select: none;">
                        <button class="btn btn-sm text-secondary border-0 p-0 shadow-none d-flex align-items-center gap-1 dropdown-toggle" data-bs-toggle="dropdown" data-bs-display="static" aria-expanded="false" onmousedown="event.preventDefault();" style="background: transparent; font-size: 0.82rem; color: #888888; cursor: pointer; border-radius: 4px; line-height: 1; user-select: none; -webkit-user-select: none;">
                            <i class="fa-solid fa-code" id="modePillIcon" style="font-size: 0.8rem; color: #888888;"></i> <span id="modePillLabel" style="font-size: 0.82rem; color: #888888;">coding mode</span>
                        </button>
                        <ul class="dropdown-menu dropdown-menu-dark shadow-lg border-secondary p-2" style="font-size: 0.84rem; min-width: 175px; background: #141414 !important; border: 1px solid #282828 !important; right: 0; left: auto !important; bottom: 100% !important; top: auto !important; margin-bottom: 6px !important; user-select: none; -webkit-user-select: none;">
                            <li><a class="dropdown-item py-2 px-3 text-secondary rounded" style="font-size: 0.84rem; color: #bbbbbb !important; user-select: none;" href="#" id="modeItemCoding" onmousedown="event.preventDefault();" onclick="setAiMode('coding')"><i class="fa-solid fa-code me-2"></i> coding mode</a></li>
                            <li><a class="dropdown-item py-2 px-3 text-secondary rounded" style="font-size: 0.84rem; color: #888888 !important; user-select: none;" href="#" id="modeItemThinking" onmousedown="event.preventDefault();" onclick="setAiMode('thinking')"><i class="fa-solid fa-brain me-2"></i> thinking mode</a></li>
                            <li><a class="dropdown-item py-2 px-3 text-secondary rounded" style="font-size: 0.84rem; color: #888888 !important; user-select: none;" href="#" id="modeItemChat" onmousedown="event.preventDefault();" onclick="setAiMode('chat')"><i class="fa-solid fa-comments me-2"></i> general chat</a></li>
                        </ul>
                    </div>

                    <button class="send-btn" id="sendBtn" onclick="sendMessage()"><i class="fa-solid fa-arrow-up"></i></button>
                </div>

                <div class="bottom-toggles-bar">
                    <div class="toggle-group align-items-center">
                        <div class="custom-toggle-pill" id="autoExecPill" onclick="toggleAutoExecute()">
                            <span class="toggle-knob"></span>
                            <span><i class="fa-solid fa-bolt me-1"></i> Auto-Run</span>
                        </div>

                        <div class="custom-toggle-pill active" id="autoFixPill" onclick="toggleAutoFix()">
                            <span class="toggle-knob"></span>
                            <span><i class="fa-solid fa-wrench me-1"></i> Auto-Fix</span>
                        </div>

                        <!-- Clean Anchored Upward Model Selector Dropdown with static CSS positioning -->
                        <div class="dropup d-inline-block" id="modelDropup">
                            <div class="custom-toggle-pill active dropdown-toggle" data-bs-toggle="dropdown" data-bs-display="static" aria-expanded="false" style="cursor: pointer;" onclick="renderModelDropupList()">
                                <i class="fa-solid fa-microchip me-1"></i> <span id="bottomModelBadge">Claude 3.5 Sonnet</span>
                            </div>
                            <ul class="dropdown-menu dropdown-menu-dark shadow-lg border-secondary" id="modelDropupList" style="font-size: 0.84rem; min-width: 280px;">
                                <!-- Dynamic Clean Rendered Items -->
                            </ul>
                        </div>
                    </div>

                    <div class="text-secondary" style="font-size: 0.78rem;">
                        <i class="fa-solid fa-shield-halved me-1"></i> Studio Mode
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Account / Login / Register Modal -->
    <div class="modal fade" id="authModal" tabindex="-1">
        <div class="modal-dialog">
            <div class="modal-content bg-dark text-light border-secondary">
                <div class="modal-header border-secondary">
                    <h5 class="modal-title" id="authModalTitle"><i class="fa-solid fa-user me-2"></i>Account Access</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div id="authLoggedOutView">
                        <ul class="nav nav-pills nav-justified mb-3" id="authTabs" role="tablist">
                            <li class="nav-item">
                                <button class="nav-link active bg-secondary text-light fw-bold me-1" id="loginTabBtn" onclick="switchAuthTab('login')">Log In</button>
                            </li>
                            <li class="nav-item">
                                <button class="nav-link bg-dark text-secondary fw-bold border border-secondary" id="registerTabBtn" onclick="switchAuthTab('register')">Register</button>
                            </li>
                        </ul>

                        <div id="loginForm">
                            <div class="mb-3">
                                <label class="form-label text-secondary" style="font-size: 0.85rem;">Username</label>
                                <input type="text" id="loginUsername" name="sf_account_username" autocomplete="username" class="form-control bg-black text-light border-secondary" placeholder="Enter username">
                            </div>
                            <div class="mb-3">
                                <label class="form-label text-secondary" style="font-size: 0.85rem;">Password</label>
                                <input type="password" id="loginPassword" name="sf_account_password" autocomplete="current-password" class="form-control bg-black text-light border-secondary" placeholder="Enter password">
                            </div>
                            <button class="btn btn-light w-100 fw-bold text-dark" onclick="submitLogin()"><i class="fa-solid fa-right-to-bracket me-1"></i> Log In</button>
                        </div>

                        <div id="registerForm" style="display: none;">
                            <div class="mb-3">
                                <label class="form-label text-secondary" style="font-size: 0.85rem;">Username</label>
                                <input type="text" id="regUsername" name="sf_reg_username" autocomplete="username" class="form-control bg-black text-light border-secondary" placeholder="Choose a username">
                            </div>
                            <div class="mb-3">
                                <label class="form-label text-secondary" style="font-size: 0.85rem;">Password</label>
                                <input type="password" id="regPassword" name="sf_reg_password" autocomplete="new-password" class="form-control bg-black text-light border-secondary" placeholder="Create a password">
                            </div>
                            <button class="btn btn-light w-100 fw-bold text-dark" onclick="submitRegister()"><i class="fa-solid fa-user-plus me-1"></i> Create Account</button>
                        </div>
                    </div>

                    <div id="authLoggedInView" style="display: none;">
                        <div class="text-center py-3">
                            <div class="avatar avatar-user mx-auto mb-2" style="width: 48px; height: 48px; font-size: 1.4rem;">
                                <i class="fa-solid fa-user-check text-light"></i>
                            </div>
                            <h5 class="fw-bold text-light mb-1" id="accountNameDisplay">Logged In User</h5>
                            <p class="text-secondary" style="font-size: 0.82rem;">Global Cloud Account Synced</p>
                        </div>

                        <div class="p-3 bg-black rounded border border-secondary mb-3" style="font-size: 0.82rem;">
                            <div class="d-flex justify-content-between mb-1">
                                <span class="text-secondary">Session Token:</span>
                                <span class="font-monospace text-light text-truncate" id="accountTokenDisplay" style="max-width: 180px;">-</span>
                            </div>
                            <div class="d-flex justify-content-between">
                                <span class="text-secondary">Cloud Sync:</span>
                                <span class="text-light fw-bold"><i class="fa-solid fa-cloud-arrow-up me-1"></i> Active</span>
                            </div>
                        </div>

                        <button class="btn btn-outline-danger w-100 fw-bold" onclick="submitLogout()"><i class="fa-solid fa-right-from-bracket me-1"></i> Log Out</button>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Clean Warning Modal -->
    <div class="modal fade" id="apiKeyWarningModal" tabindex="-1">
        <div class="modal-dialog">
            <div class="modal-content bg-dark text-light border-secondary">
                <div class="modal-header border-secondary">
                    <h5 class="modal-title text-warning"><i class="fa-solid fa-triangle-exclamation me-2"></i>OpenRouter API Key Required</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <p class="text-light mb-3" id="warningModalText" style="font-size: 0.92rem;">
                        OpenRouter API key is not connected yet. Please add your key in Settings.
                    </p>
                    <div class="p-3 bg-black rounded border border-secondary text-secondary" style="font-size: 0.82rem;">
                        <i class="fa-solid fa-circle-info text-light me-1"></i> Connecting your OpenRouter API key unlocks Claude, GPT-4o, DeepSeek, Qwen, and Gemma models.
                    </div>
                </div>
                <div class="modal-footer border-secondary">
                    <button type="button" class="btn btn-light fw-bold px-4 text-dark" onclick="openSettingsModalFromWarning()"><i class="fa-solid fa-gear me-1"></i> Open Settings</button>
                    <button type="button" class="btn btn-outline-secondary text-light px-3" data-bs-dismiss="modal">Close</button>
                </div>
            </div>
        </div>
    </div>

    <!-- Executor Loader Modal -->
    <div class="modal fade" id="executorModal" tabindex="-1">
        <div class="modal-dialog modal-lg">
            <div class="modal-content bg-dark text-light border-secondary">
                <div class="modal-header border-secondary">
                    <h5 class="modal-title"><i class="fa-solid fa-shield-halved text-light me-2"></i>Connect Roblox Executor (Unique Session Token)</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <p style="font-size: 0.88rem;" class="text-secondary">
                        Copy and execute your <strong class="text-light">unique multi-line session loadstring</strong> below in your executor (<strong class="text-light">Solara, Wave, Delta, MacSploit</strong>). Your session is protected by an isolated 32-character crypto key so no one else can hijack your executor session!
                    </p>

                    <div class="code-container my-3">
                        <div class="code-header">
                            <span><i class="fa-solid fa-key me-1"></i> Player-Specific Session Loadstring</span>
                            <div>
                                <button class="btn btn-sm btn-light text-dark fw-bold py-0 px-2" style="font-size: 0.78rem;" onclick="copyExecutorScript()">
                                    <i class="fa-solid fa-copy me-1"></i> Copy Loadstring
                                </button>
                            </div>
                        </div>
                        <pre><code class="language-lua" id="executorScriptCode">-- ScriptForge Unique Session Loader
getgenv().SCRIPTFORGE_URL = "${window.location.origin}"
getgenv().SESSION_KEY = "${getSessionKey()}"

loadstring(game:HttpGet("https://raw.githubusercontent.com/blockysz/ScriptForge/main/roblox_client.lua"))()</code></pre>
                    </div>

                    <div class="p-2 bg-black rounded border border-secondary text-secondary" style="font-size: 0.8rem;">
                        <i class="fa-solid fa-shield-cat text-light me-1"></i> <strong>Session Protection:</strong> Your unique Session Token ensures your Roblox client and AI chat session are 100% private and isolated.
                    </div>
                </div>
                <div class="modal-footer border-secondary">
                    <button type="button" class="btn btn-light fw-bold px-4 text-dark" onclick="copyExecutorScript()"><i class="fa-solid fa-copy me-1"></i> Copy Code</button>
                    <button type="button" class="btn btn-outline-secondary text-light px-3" data-bs-dismiss="modal">Close</button>
                </div>
            </div>
        </div>
    </div>

    <!-- Clean Vertical Settings Page (OpenRouter Only) -->
    <div class="modal fade" id="settingsModal" tabindex="-1">
        <div class="modal-dialog modal-lg">
            <div class="modal-content bg-dark text-light border-secondary">
                <div class="modal-header border-secondary">
                    <h5 class="modal-title"><i class="fa-solid fa-gear me-2"></i> API Keys & Studio Settings</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body p-4">
                    <div class="d-flex flex-column gap-4">
                        <!-- Single Unified OpenRouter API Key Input -->
                        <div class="p-3 bg-black rounded border border-secondary">
                            <div class="d-flex justify-content-between align-items-center mb-1">
                                <label class="form-label font-weight-bold text-light mb-0">
                                    <i class="fa-solid fa-globe text-light me-2"></i> OpenRouter API Key
                                </label>
                                <a href="https://openrouter.ai/keys" target="_blank" class="btn btn-sm btn-outline-light font-monospace py-0 px-2" style="font-size: 0.78rem;">
                                    <i class="fa-solid fa-arrow-up-right-from-square me-1"></i> Get OpenRouter Key
                                </a>
                            </div>
                            <p class="text-secondary mb-2" style="font-size: 0.78rem;">Single key powers all models (Claude 3.5 Sonnet, GPT-4o, DeepSeek Coder V2, Qwen 2.5 Coder, Gemma 4).</p>
                            <input type="password" id="openrouterApiKeyInput" class="form-control bg-dark text-light border-secondary font-monospace" placeholder="sk-or-v1-...">
                        </div>

                        <!-- Sync & Storage Scope -->
                        <div class="p-3 bg-black rounded border border-secondary">
                            <div class="d-flex justify-content-between align-items-center">
                                <div>
                                    <h6 class="mb-1 text-light fw-bold"><i class="fa-solid fa-cloud-arrow-up me-2"></i> Settings Storage Scope</h6>
                                    <span class="text-secondary" style="font-size: 0.8rem;" id="settingsSyncScopeText">Saved locally to browser storage</span>
                                </div>
                                <span class="badge bg-dark border border-secondary text-light font-monospace" id="settingsScopeBadge">Local Browser</span>
                            </div>
                        </div>

                        <!-- Save Button at Bottom -->
                        <div class="pt-2">
                            <button type="button" class="btn btn-light w-100 fw-bold py-2 text-dark" onclick="saveSettings()">
                                <i class="fa-solid fa-floppy-disk me-2"></i> Save All Settings
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.7.0/highlight.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/4.3.0/marked.min.js"></script>
    
    <script>
        // Account State
        let loggedInUser = localStorage.getItem("SCRIPTFORGE_USER") || null;

        function getSessionKey() {
            let key = localStorage.getItem("SCRIPTFORGE_SESSION_KEY");
            if (!key) {
                const arr = new Uint8Array(16);
                window.crypto.getRandomValues(arr);
                key = "sf_live_" + Array.from(arr, b => b.toString(16).padStart(2, '0')).join('');
                localStorage.setItem("SCRIPTFORGE_SESSION_KEY", key);
            }
            return key;
        }

        let openrouterApiKey = localStorage.getItem("OPENROUTER_API_KEY") || "";
        let selectedModel = localStorage.getItem("ANTIGRAVITY_SELECTED_MODEL") || "openrouter/anthropic/claude-3.5-sonnet";
        let aiMode = localStorage.getItem("SCRIPTFORGE_AI_MODE") || "coding";
        let autoExecute = localStorage.getItem("ANTIGRAVITY_AUTO_EXECUTE") === "true";
        let autoFix = localStorage.getItem("ANTIGRAVITY_AUTO_FIX") !== "false";
        let currentTheme = localStorage.getItem("ANTIGRAVITY_THEME") || "dark";
        let wasConnected = false;
        let currentGameName = "General Roblox";
        let currentPlaceId = 0;
        let editingChatId = null;
        let activePollingScriptIds = {};

        // Clean Model Definitions (Emojis & Icons Removed)
        const ALL_MODELS = [
            { id: "openrouter/anthropic/claude-3.5-sonnet", name: "Claude 3.5 Sonnet" },
            { id: "openrouter/anthropic/claude-3-haiku", name: "Claude 3 Haiku" },
            { id: "openrouter/openai/gpt-4o", name: "GPT-4o" },
            { id: "openrouter/openai/gpt-4o-mini", name: "GPT-4o Mini" },
            { id: "openrouter/deepseek/deepseek-coder", name: "DeepSeek Coder V2" },
            { id: "openrouter/qwen/qwen-2.5-coder-32b-instruct", name: "Qwen 2.5 Coder 32B" },
            { id: "openrouter/google/gemma-4-31b-it:free", name: "Gemma 4 (Free)" },
            { id: "openrouter/cohere/north-mini-code:free", name: "Cohere North Code (Free)" }
        ];

        document.documentElement.setAttribute("data-bs-theme", currentTheme);
        updateThemeIcon();
        updateAuthHeaderBtn();
        updateModelDisplayBadge();
        updateTogglePillsUI();
        updateAiModeUI();

        function updateAiModeUI() {
            const labelEl = document.getElementById("modePillLabel");
            const iconEl = document.getElementById("modePillIcon");
            
            document.querySelectorAll("#modeInputDropup .dropdown-item").forEach(item => item.style.fontWeight = "normal");

            if (aiMode === "thinking") {
                labelEl.innerText = "thinking mode";
                iconEl.className = "fa-solid fa-brain";
                const el = document.getElementById("modeItemThinking");
                if (el) el.style.fontWeight = "600";
            } else if (aiMode === "chat") {
                labelEl.innerText = "general chat";
                iconEl.className = "fa-solid fa-comments";
                const el = document.getElementById("modeItemChat");
                if (el) el.style.fontWeight = "600";
            } else {
                aiMode = "coding";
                labelEl.innerText = "coding mode";
                iconEl.className = "fa-solid fa-code";
                const el = document.getElementById("modeItemCoding");
                if (el) el.style.fontWeight = "600";
            }
        }

        function setAiMode(mode) {
            aiMode = mode;
            localStorage.setItem("SCRIPTFORGE_AI_MODE", aiMode);
            updateAiModeUI();
            
            const modeNames = { coding: "coding mode", thinking: "thinking mode", chat: "general chat" };
            showToast("Mode: " + modeNames[mode], "fa-solid fa-sliders text-light");
        }

        function getCleanModelName(id) {
            const m = ALL_MODELS.find(x => x.id === id);
            return m ? m.name : id.split('/').pop();
        }

        function updateModelDisplayBadge() {
            document.getElementById("bottomModelBadge").innerText = getCleanModelName(selectedModel);
        }

        function isModelConnected() {
            return !!openrouterApiKey;
        }

        function renderModelDropupList() {
            const listEl = document.getElementById("modelDropupList");
            listEl.innerHTML = "";

            const connected = isModelConnected();

            if (connected) {
                const header1 = document.createElement("li");
                header1.innerHTML = `<h6 class="dropdown-header text-white fw-bold font-monospace">MODELS</h6>`;
                listEl.appendChild(header1);

                ALL_MODELS.forEach(m => {
                    const li = document.createElement("li");
                    const isSelected = m.id === selectedModel;
                    li.innerHTML = `<a class="dropdown-item py-2 d-flex justify-content-between align-items-center ${isSelected ? 'active fw-bold' : ''}" href="#" onclick="selectModel('${m.id}')"><span>${escapeHtml(m.name)}</span> ${isSelected ? '<i class="fa-solid fa-check text-success"></i>' : ''}</a>`;
                    listEl.appendChild(li);
                });
            } else {
                const header2 = document.createElement("li");
                header2.innerHTML = `<h6 class="dropdown-header text-warning font-monospace">OPENROUTER KEY REQUIRED</h6>`;
                listEl.appendChild(header2);

                ALL_MODELS.forEach(m => {
                    const li = document.createElement("li");
                    li.innerHTML = `<a class="dropdown-item py-2 d-flex justify-content-between align-items-center text-secondary" href="#" onclick="clickUnconnectedModel('${m.id}', '${escapeHtml(m.name)}')"><span>${escapeHtml(m.name)}</span> <span class="badge bg-dark border border-warning text-warning" style="font-size:0.68rem;">Needs Key</span></a>`;
                    listEl.appendChild(li);
                });
            }
        }

        function selectModel(modelId) {
            selectedModel = modelId;
            localStorage.setItem("ANTIGRAVITY_SELECTED_MODEL", selectedModel);
            updateModelDisplayBadge();
            showToast("Model: " + getCleanModelName(selectedModel), "fa-solid fa-microchip text-light");
        }

        function clickUnconnectedModel(modelId, modelName) {
            document.getElementById("warningModalText").innerText = `OpenRouter API key is not connected yet. Please add your key in Settings to use ${modelName}.`;
            const modal = new bootstrap.Modal(document.getElementById("apiKeyWarningModal"));
            modal.show();
        }

        function openSettingsModalFromWarning() {
            bootstrap.Modal.getInstance(document.getElementById("apiKeyWarningModal")).hide();
            openSettingsModal();
        }

        function openSettingsModal() {
            document.getElementById("openrouterApiKeyInput").value = openrouterApiKey;

            const scopeText = document.getElementById("settingsSyncScopeText");
            const scopeBadge = document.getElementById("settingsScopeBadge");

            if (loggedInUser) {
                scopeText.innerText = `Synced to Cloud Account (${loggedInUser})`;
                scopeBadge.innerText = "Account Synced";
                scopeBadge.className = "badge bg-success text-white font-monospace";
            } else {
                scopeText.innerText = "Saved locally to browser storage";
                scopeBadge.innerText = "Local Browser";
                scopeBadge.className = "badge bg-dark border border-secondary text-light font-monospace";
            }

            const modal = new bootstrap.Modal(document.getElementById("settingsModal"));
            modal.show();
        }

        function saveSettings() {
            openrouterApiKey = document.getElementById("openrouterApiKeyInput").value.trim();
            localStorage.setItem("OPENROUTER_API_KEY", openrouterApiKey);

            fetch('/api/set_key', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ openrouter_key: openrouterApiKey })
            });

            bootstrap.Modal.getInstance(document.getElementById('settingsModal')).hide();
            showToast("Settings Saved!", "fa-solid fa-floppy-disk text-light");
            renderModelDropupList();
        }

        function updateAuthHeaderBtn() {
            const btn = document.getElementById("authHeaderBtn");
            if (loggedInUser) {
                btn.innerHTML = `<i class="fa-solid fa-user-check me-1"></i> ${escapeHtml(loggedInUser)}`;
            } else {
                btn.innerHTML = `<i class="fa-solid fa-user me-1"></i> Login / Sign Up`;
            }
        }

        function openAuthModal() {
            if (loggedInUser) {
                document.getElementById("authLoggedOutView").style.display = "none";
                document.getElementById("authLoggedInView").style.display = "block";
                document.getElementById("accountNameDisplay").innerText = loggedInUser;
                document.getElementById("accountTokenDisplay").innerText = getSessionKey();
            } else {
                document.getElementById("authLoggedOutView").style.display = "block";
                document.getElementById("authLoggedInView").style.display = "none";
            }
            const modal = new bootstrap.Modal(document.getElementById("authModal"));
            modal.show();
        }

        function switchAuthTab(tab) {
            const loginBtn = document.getElementById("loginTabBtn");
            const regBtn = document.getElementById("registerTabBtn");
            const loginForm = document.getElementById("loginForm");
            const regForm = document.getElementById("registerForm");

            if (tab === 'login') {
                loginBtn.className = "nav-link active bg-secondary text-light fw-bold me-1";
                regBtn.className = "nav-link bg-dark text-secondary fw-bold border border-secondary";
                loginForm.style.display = "block";
                regForm.style.display = "none";
            } else {
                regBtn.className = "nav-link active bg-secondary text-light fw-bold";
                loginBtn.className = "nav-link bg-dark text-secondary fw-bold border border-secondary me-1";
                regForm.style.display = "block";
                loginForm.style.display = "none";
            }
        }

        async function submitLogin() {
            const user = document.getElementById("loginUsername").value.trim();
            const pass = document.getElementById("loginPassword").value;
            if (!user || !pass) {
                showToast("Please enter username and password", "fa-solid fa-circle-exclamation text-secondary");
                return;
            }

            try {
                const res = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ username: user, password: pass })
                });

                const data = await res.json();
                if (data.error) {
                    showToast(data.error, "fa-solid fa-circle-exclamation text-secondary");
                } else {
                    loggedInUser = data.user.username;
                    localStorage.setItem("SCRIPTFORGE_USER", loggedInUser);
                    if (data.user.session_key) {
                        localStorage.setItem("SCRIPTFORGE_SESSION_KEY", data.user.session_key);
                    }
                    if (data.user.chats && data.user.chats.length > 0) {
                        chats = data.user.chats;
                        activeChatId = chats[0].id;
                        saveChatsToStorage();
                        renderChatList();
                        renderActiveChat();
                    }
                    updateAuthHeaderBtn();
                    bootstrap.Modal.getInstance(document.getElementById("authModal")).hide();
                    showToast(`Welcome back, ${loggedInUser}!`, "fa-solid fa-circle-check text-light");
                }
            } catch(e) {
                showToast("Connection error: " + e.message, "fa-solid fa-circle-exclamation text-secondary");
            }
        }

        async function submitRegister() {
            const user = document.getElementById("regUsername").value.trim();
            const pass = document.getElementById("regPassword").value;
            if (!user || !pass) {
                showToast("Please choose a username and password", "fa-solid fa-circle-exclamation text-secondary");
                return;
            }

            try {
                const res = await fetch('/api/auth/register', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ username: user, password: pass, session_key: getSessionKey() })
                });

                const data = await res.json();
                if (data.error) {
                    showToast(data.error, "fa-solid fa-circle-exclamation text-secondary");
                } else {
                    loggedInUser = data.user.username;
                    localStorage.setItem("SCRIPTFORGE_USER", loggedInUser);
                    updateAuthHeaderBtn();
                    bootstrap.Modal.getInstance(document.getElementById("authModal")).hide();
                    showToast(`Account created! Welcome ${loggedInUser}`, "fa-solid fa-circle-check text-light");
                    syncChatsToCloud();
                }
            } catch(e) {
                showToast("Connection error: " + e.message, "fa-solid fa-circle-exclamation text-secondary");
            }
        }

        function submitLogout() {
            loggedInUser = null;
            localStorage.removeItem("SCRIPTFORGE_USER");
            updateAuthHeaderBtn();
            bootstrap.Modal.getInstance(document.getElementById("authModal")).hide();
            showToast("Logged out of account", "fa-solid fa-circle-info text-secondary");
        }

        async function syncChatsToCloud() {
            if (!loggedInUser) return;
            try {
                await fetch('/api/auth/sync_chats', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        username: loggedInUser,
                        session_key: getSessionKey(),
                        chats: chats
                    })
                });
            } catch(e) {
                console.error("Cloud sync error:", e);
            }
        }

        function updateTogglePillsUI() {
            const execPill = document.getElementById("autoExecPill");
            const fixPill = document.getElementById("autoFixPill");
            if (autoExecute) execPill.classList.add("active"); else execPill.classList.remove("active");
            if (autoFix) fixPill.classList.add("active"); else fixPill.classList.remove("active");
        }

        function toggleTheme() {
            currentTheme = currentTheme === "dark" ? "light" : "dark";
            document.documentElement.setAttribute("data-bs-theme", currentTheme);
            localStorage.setItem("ANTIGRAVITY_THEME", currentTheme);
            updateThemeIcon();
        }

        function updateThemeIcon() {
            const btn = document.getElementById("themeBtn");
            btn.innerHTML = currentTheme === "dark" ? '<i class="fa-solid fa-sun"></i>' : '<i class="fa-solid fa-moon"></i>';
        }

        function toggleAutoExecute() {
            autoExecute = !autoExecute;
            localStorage.setItem("ANTIGRAVITY_AUTO_EXECUTE", autoExecute);
            updateTogglePillsUI();
            showToast(autoExecute ? "Auto-Run Enabled" : "Auto-Run Disabled", autoExecute ? "fa-solid fa-bolt text-light" : "fa-solid fa-circle-info text-secondary");
        }

        function toggleAutoFix() {
            autoFix = !autoFix;
            localStorage.setItem("ANTIGRAVITY_AUTO_FIX", autoFix);
            updateTogglePillsUI();
            showToast(autoFix ? "Auto-Fix Errors Active" : "Auto-Fix Disabled", autoFix ? "fa-solid fa-wrench text-light" : "fa-solid fa-circle-info text-secondary");
        }

        function autoGrow(element) {
            element.style.height = "24px";
            element.style.height = Math.min(element.scrollHeight, 120) + "px";
        }

        function getFormattedExecutorScript() {
            const origin = window.location.origin;
            const key = getSessionKey();
            return `-- ScriptForge Unique Session Loader\ngetgenv().SCRIPTFORGE_URL = "${origin}"\ngetgenv().SESSION_KEY = "${key}"\n\nloadstring(game:HttpGet("https://raw.githubusercontent.com/blockysz/ScriptForge/main/roblox_client.lua"))()`;
        }

        function openExecutorModal() {
            const codeEl = document.getElementById("executorScriptCode");
            codeEl.innerText = getFormattedExecutorScript();
            hljs.highlightElement(codeEl);
            const modal = new bootstrap.Modal(document.getElementById("executorModal"));
            modal.show();
        }

        function copyExecutorScript() {
            const code = getFormattedExecutorScript();
            navigator.clipboard.writeText(code);
            showToast("Session Loadstring copied!", "fa-solid fa-copy text-light");
        }

        let chats = JSON.parse(localStorage.getItem("SCRIPTFORGE_CHATS")) || JSON.parse(localStorage.getItem("ANTIGRAVITY_CHATS")) || [];
        let activeChatId = localStorage.getItem("SCRIPTFORGE_ACTIVE_CHAT") || localStorage.getItem("ANTIGRAVITY_ACTIVE_CHAT") || null;

        if (chats.length === 0) {
            createNewChat(false);
        } else if (!activeChatId || !chats.find(c => c.id === activeChatId)) {
            activeChatId = chats[0].id;
        }

        function saveChatsToStorage() {
            localStorage.setItem("SCRIPTFORGE_CHATS", JSON.stringify(chats));
            localStorage.setItem("SCRIPTFORGE_ACTIVE_CHAT", activeChatId);
            syncChatsToCloud();
        }

        function createNewChat(switchChat = true) {
            const newChat = {
                id: 'chat_' + Date.now(),
                title: 'New chat',
                game_name: currentGameName,
                place_id: currentPlaceId,
                messages: [
                    {
                        role: 'ai',
                        content: "Welcome to ScriptForge. Connect your Roblox executor script to start writing & auto-fixing game scripts in real-time."
                    }
                ]
            };
            chats.unshift(newChat);
            if (switchChat) activeChatId = newChat.id;
            saveChatsToStorage();
            updateGameFilterDropdown();
            renderChatList();
            renderActiveChat();
        }

        function switchChat(id) {
            if (editingChatId === id) return;
            activeChatId = id;
            editingChatId = null;
            saveChatsToStorage();
            renderChatList();
            renderActiveChat();
        }

        function startInlineRename(id, e) {
            if (e) e.stopPropagation();
            editingChatId = id;
            renderChatList();
            setTimeout(() => {
                const input = document.getElementById('rename_input_' + id);
                if (input) {
                    input.focus();
                    input.select();
                }
            }, 50);
        }

        function saveInlineRename(id, newTitle) {
            const chat = chats.find(c => c.id === id);
            if (chat && newTitle && newTitle.trim()) {
                chat.title = newTitle.trim();
                saveChatsToStorage();
            }
            editingChatId = null;
            renderChatList();
        }

        function cancelInlineRename() {
            editingChatId = null;
            renderChatList();
        }

        function deleteChat(id, e) {
            e.stopPropagation();
            if (chats.length <= 1) {
                showToast("Cannot delete active chat", "fa-solid fa-circle-exclamation text-secondary");
                return;
            }
            chats = chats.filter(c => c.id !== id);
            if (activeChatId === id) activeChatId = chats[0].id;
            saveChatsToStorage();
            updateGameFilterDropdown();
            renderChatList();
            renderActiveChat();
        }

        function updateGameFilterDropdown() {
            const select = document.getElementById("gameFilterSelect");
            const currentVal = select.value;
            select.innerHTML = '<option value="all">All Games</option>';
            
            const set = new Set();
            chats.forEach(c => {
                if (c.game_name) set.add(c.game_name);
            });

            set.forEach(gName => {
                const opt = document.createElement("option");
                opt.value = gName;
                opt.innerText = gName;
                select.appendChild(opt);
            });

            if (Array.from(select.options).some(o => o.value === currentVal)) {
                select.value = currentVal;
            }
        }

        function renderChatList() {
            const listEl = document.getElementById("chatList");
            listEl.innerHTML = "";

            const searchQuery = document.getElementById("chatSearchInput").value.trim().toLowerCase();
            const selectedGame = document.getElementById("gameFilterSelect").value;

            let filteredChats = chats.filter(c => {
                if (selectedGame !== "all" && c.game_name !== selectedGame) return false;
                return true;
            });

            if (!searchQuery) {
                filteredChats.forEach(chat => {
                    listEl.appendChild(createChatItemHTML(chat));
                });
                return;
            }

            const titleMatches = [];
            const messageMatches = [];

            filteredChats.forEach(chat => {
                const titleMatch = chat.title.toLowerCase().includes(searchQuery);
                let matchedSnippet = null;

                for (let msg of chat.messages) {
                    if (msg.content && msg.content.toLowerCase().includes(searchQuery)) {
                        const idx = msg.content.toLowerCase().indexOf(searchQuery);
                        const start = Math.max(0, idx - 15);
                        const end = Math.min(msg.content.length, idx + searchQuery.length + 20);
                        matchedSnippet = "..." + msg.content.substring(start, end).replace(/\n/g, " ") + "...";
                        break;
                    }
                }

                if (titleMatch) {
                    titleMatches.push(chat);
                } else if (matchedSnippet) {
                    messageMatches.push({ chat: chat, snippet: matchedSnippet });
                }
            });

            if (titleMatches.length > 0) {
                const div1 = document.createElement("div");
                div1.className = "section-divider";
                div1.innerText = "Matching Chats";
                listEl.appendChild(div1);

                titleMatches.forEach(chat => {
                    listEl.appendChild(createChatItemHTML(chat));
                });
            }

            if (messageMatches.length > 0) {
                const div2 = document.createElement("div");
                div2.className = "section-divider";
                div2.innerText = "Messages Found";
                listEl.appendChild(div2);

                messageMatches.forEach(item => {
                    listEl.appendChild(createChatItemHTML(item.chat, item.snippet));
                });
            }

            if (titleMatches.length === 0 && messageMatches.length === 0) {
                const noRes = document.createElement("div");
                noRes.className = "text-center text-secondary py-3";
                noRes.style.fontSize = "0.8rem";
                noRes.innerText = "No chats or messages found";
                listEl.appendChild(noRes);
            }
        }

        function createChatItemHTML(chat, snippet = null) {
            const item = document.createElement("div");
            item.className = "chat-item " + (chat.id === activeChatId ? "active" : "");
            item.onclick = () => switchChat(chat.id);
            item.ondblclick = (e) => startInlineRename(chat.id, e);
            
            let gameTag = chat.game_name ? `<span class="chat-item-sub">${escapeHtml(chat.game_name)}</span>` : '';
            let snippetTag = snippet ? `<span class="chat-item-sub text-light">${escapeHtml(snippet)}</span>` : '';

            let titleHTML = '';
            if (editingChatId === chat.id) {
                titleHTML = `
                    <input type="text" id="rename_input_${chat.id}" class="inline-rename-input"
                           value="${escapeHtml(chat.title)}"
                           onclick="event.stopPropagation()"
                           onkeydown="if(event.key==='Enter') saveInlineRename('${chat.id}', this.value); if(event.key==='Escape') cancelInlineRename();"
                           onblur="saveInlineRename('${chat.id}', this.value)">
                `;
            } else {
                titleHTML = `<div class="chat-item-title">${escapeHtml(chat.title)}</div>`;
            }

            item.innerHTML = `
                <div class="chat-item-title-box">
                    ${titleHTML}
                    ${snippetTag || gameTag}
                </div>
                <div class="chat-item-actions">
                    <span class="chat-action-btn" onclick="startInlineRename('${chat.id}', event)" title="Rename"><i class="fa-solid fa-pencil"></i></span>
                    <span class="chat-action-btn" onclick="deleteChat('${chat.id}', event)" title="Delete"><i class="fa-solid fa-trash-can"></i></span>
                </div>
            `;
            return item;
        }

        function renderActiveChat() {
            const chat = chats.find(c => c.id === activeChatId);
            const container = document.getElementById("chatMessages");
            container.innerHTML = "";
            if (!chat) return;

            chat.messages.forEach(msg => {
                const row = document.createElement("div");
                row.className = "message-row";
                
                const avatar = document.createElement("div");
                avatar.className = "avatar " + (msg.role === 'user' ? 'avatar-user' : 'avatar-ai');
                avatar.innerHTML = msg.role === 'user' ? '<i class="fa-solid fa-user"></i>' : '<i class="fa-solid fa-robot"></i>';

                const content = document.createElement("div");
                content.className = "message-content";

                if (msg.role === 'ai') {
                    content.innerHTML = marked.parse(msg.content);
                    formatCodeBlocks(content);

                    if (msg.trajectory && msg.trajectory.length > 0) {
                        const trajBox = document.createElement("div");
                        trajBox.className = "agent-trajectory-card";
                        
                        let trajHtml = `<div class="agent-trajectory-header"><i class="fa-solid fa-gear fa-spin text-light"></i> Agent Verification Trajectory:</div>`;
                        msg.trajectory.forEach(log => {
                            trajHtml += `<div class="agent-step">${escapeHtml(log)}</div>`;
                        });
                        trajBox.innerHTML = trajHtml;
                        content.appendChild(trajBox);
                    }
                } else {
                    content.innerText = msg.content;
                }

                row.appendChild(avatar);
                row.appendChild(content);
                container.appendChild(row);
            });
            container.scrollTop = container.scrollHeight;
        }

        function showToast(message, iconClass = "fa-solid fa-circle-check text-light") {
            const popup = document.getElementById("toastPopup");
            const icon = document.getElementById("toastIcon");
            const msg = document.getElementById("toastMsg");
            icon.className = iconClass;
            msg.innerText = message;
            popup.classList.add("show");
            setTimeout(() => {
                popup.classList.remove("show");
            }, 2500);
        }

        async function updateStatus() {
            try {
                const sessionKey = getSessionKey();
                const res = await fetch('/api/status?session_key=' + encodeURIComponent(sessionKey));
                if (!res.ok) return;
                const data = await res.json();
                
                const badge = document.getElementById('statusBadge');
                if (data.connected) {
                    badge.className = 'status-badge status-online';
                    badge.innerHTML = '<span class="dot dot-online"></span> Connected';
                    document.getElementById('statPlayer').innerText = data.player_name || "-";
                    document.getElementById('statRemotes').innerText = (data.remotes ? data.remotes.length : 0);

                    if (data.place_id && data.place_id !== currentPlaceId) {
                        currentPlaceId = data.place_id;
                        fetch('/api/get_game_name/' + data.place_id)
                            .then(r => r.json())
                            .then(gData => {
                                currentGameName = gData.game_name || ("Place " + data.place_id);
                                document.getElementById('statGame').innerText = currentGameName;
                                
                                const activeChat = chats.find(c => c.id === activeChatId);
                                if (activeChat && (!activeChat.game_name || activeChat.game_name === "General Roblox")) {
                                    activeChat.game_name = currentGameName;
                                    activeChat.place_id = currentPlaceId;
                                    saveChatsToStorage();
                                    updateGameFilterDropdown();
                                    renderChatList();
                                }
                            });
                    }

                    if (!wasConnected) {
                        wasConnected = true;
                        showToast(`Connected to ${data.player_name || "Roblox"}`, "fa-solid fa-gamepad text-light");
                        
                        const modalEl = document.getElementById("executorModal");
                        if (modalEl && modalEl.classList.contains("show")) {
                            const modalInstance = bootstrap.Modal.getInstance(modalEl);
                            if (modalInstance) modalInstance.hide();
                        }
                    }
                } else {
                    badge.className = 'status-badge status-offline';
                    badge.innerHTML = '<span class="dot dot-offline"></span> Disconnected';
                    document.getElementById('statPlayer').innerText = "-";
                    document.getElementById('statGame').innerText = "-";
                    document.getElementById('statRemotes').innerText = "0";

                    if (wasConnected) {
                        wasConnected = false;
                        showToast("Roblox Session Disconnected", "fa-solid fa-circle-exclamation text-secondary");
                    }
                }
            } catch (e) {
                console.error(e);
            }
        }
        setInterval(updateStatus, 2000);
        updateStatus();

        function handleKeyDown(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        }

        function sendQuickPrompt(promptText) {
            document.getElementById("userInput").value = promptText;
            sendMessage();
        }

        async function sendMessage() {
            const input = document.getElementById('userInput');
            const prompt = input.value.trim();
            if (!prompt) return;

            if (!isModelConnected()) {
                clickUnconnectedModel(selectedModel, getCleanModelName(selectedModel));
                return;
            }

            const chat = chats.find(c => c.id === activeChatId);
            if (!chat) return;

            const isFirstMessage = (chat.title === "New chat" || chat.messages.length <= 1);

            chat.messages.push({ role: 'user', content: prompt });
            if (!chat.game_name || chat.game_name === "General Roblox") {
                chat.game_name = currentGameName;
                chat.place_id = currentPlaceId;
            }
            saveChatsToStorage();
            renderActiveChat();
            input.value = '';
            input.style.height = "24px";

            if (isFirstMessage) {
                fetch('/api/generate_title', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ prompt: prompt })
                }).then(r => r.json()).then(tData => {
                    if (tData.title) {
                        chat.title = tData.title;
                        saveChatsToStorage();
                        updateGameFilterDropdown();
                        renderChatList();
                    }
                });
            }

            const container = document.getElementById('chatMessages');
            const progressRow = document.createElement('div');
            progressRow.className = 'message-row';
            progressRow.innerHTML = `
                <div class="avatar avatar-ai"><i class="fa-solid fa-robot"></i></div>
                <div class="message-content">
                    <div class="progress-box">
                        <i class="fa-solid fa-spinner fa-spin me-2"></i>
                        <span>Processing request with ${escapeHtml(getCleanModelName(selectedModel))}...</span>
                    </div>
                </div>
            `;
            container.appendChild(progressRow);
            container.scrollTop = container.scrollHeight;

            try {
                const historyForApi = chat.messages.slice(0, -1);

                const res = await fetch('/api/chat', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        session_key: getSessionKey(),
                        prompt: prompt,
                        openrouter_key: openrouterApiKey,
                        model: selectedModel,
                        ai_mode: aiMode,
                        auto_execute: autoExecute,
                        auto_fix: autoFix,
                        history: historyForApi
                    })
                });

                const rawText = await res.text();
                let data;
                try {
                    data = JSON.parse(rawText);
                } catch (e) {
                    data = { error: "Server returned non-JSON response: " + rawText.substring(0, 150) };
                }

                if (progressRow.parentNode) container.removeChild(progressRow);

                if (data.error) {
                    chat.messages.push({ role: 'ai', content: '❌ **Error:** ' + data.error });
                    saveChatsToStorage();
                    renderActiveChat();
                } else {
                    const aiMsgObj = {
                        role: 'ai',
                        content: data.reply,
                        script_id: data.script_id || null,
                        trajectory: data.trajectory || []
                    };
                    chat.messages.push(aiMsgObj);
                    saveChatsToStorage();
                    renderActiveChat();

                    if (data.script_id && data.status === "verifying") {
                        startScriptVerificationPolling(chat.id, chat.messages.length - 1, data.script_id);
                    }
                }

            } catch (err) {
                if (progressRow.parentNode) container.removeChild(progressRow);
                chat.messages.push({ role: 'ai', content: '❌ **Connection Error:** ' + err.message });
                saveChatsToStorage();
                renderActiveChat();
            }
        }

        function startScriptVerificationPolling(chatId, messageIdx, scriptId) {
            if (activePollingScriptIds[scriptId]) return;
            activePollingScriptIds[scriptId] = true;

            const pollInterval = setInterval(async () => {
                try {
                    const res = await fetch(`/api/script_status/${scriptId}?session_key=${encodeURIComponent(getSessionKey())}`);
                    if (!res.ok) return;
                    const statusData = await res.json();

                    const chat = chats.find(c => c.id === chatId);
                    if (chat && chat.messages[messageIdx]) {
                        const msg = chat.messages[messageIdx];
                        msg.trajectory = statusData.logs || [];

                        if (statusData.status === "verified" || statusData.status === "failed") {
                            clearInterval(pollInterval);
                            delete activePollingScriptIds[scriptId];

                            if (statusData.final_code) {
                                msg.content = statusData.reply || `✅ **Script Verified (0 Errors)**\n\`\`\`luau\n${statusData.final_code}\n\`\`\``;
                            }
                        }

                        saveChatsToStorage();
                        if (activeChatId === chatId) renderActiveChat();
                    }
                } catch(e) {
                    console.error("Polling error:", e);
                }
            }, 1000);
        }

        function formatCodeBlocks(container) {
            container.querySelectorAll('pre code').forEach((block) => {
                hljs.highlightElement(block);
                const codeText = block.innerText;
                
                const codeBox = document.createElement('div');
                codeBox.className = 'code-container';
                
                const header = document.createElement('div');
                header.className = 'code-header';
                header.innerHTML = `
                    <span>luau</span>
                    <div>
                        <button class="btn btn-sm btn-light me-1 text-dark py-0 px-2 fw-bold" style="font-size: 0.75rem;" onclick="runInRoblox(this)">
                            Run in Game
                        </button>
                        <button class="btn btn-sm btn-outline-secondary py-0 px-2 text-light" style="font-size: 0.75rem;" onclick="copyCode(this)">
                            Copy
                        </button>
                    </div>
                `;
                header.setAttribute('data-code', codeText);
                
                block.parentNode.parentNode.insertBefore(codeBox, block.parentNode);
                codeBox.appendChild(header);
                codeBox.appendChild(block.parentNode);
            });
        }

        async function runInRoblox(btn) {
            const code = btn.closest('.code-header').getAttribute('data-code');
            try {
                await fetch('/api/queue_script', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({session_key: getSessionKey(), code: code})
                });
                showToast("Script queued to Roblox Executor", "fa-solid fa-rocket text-light");
            } catch(e) {
                showToast("Error sending script: " + e.message, "fa-solid fa-circle-exclamation text-secondary");
            }
        }

        function copyCode(btn) {
            const code = btn.closest('.code-header').getAttribute('data-code');
            navigator.clipboard.writeText(code);
            showToast("Code copied to clipboard", "fa-solid fa-copy text-light");
        }

        function escapeHtml(str) {
            return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        }

        renderModelDropupList();
        updateGameFilterDropdown();
        renderChatList();
        renderActiveChat();
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

# Authentication Endpoints
@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    session_key = data.get("session_key") or ""

    if not username or len(username) < 3:
        return jsonify({"error": "Username must be at least 3 characters"}), 400
    if not password or len(password) < 4:
        return jsonify({"error": "Password must be at least 4 characters"}), 400

    accounts = load_accounts()
    user_key = username.lower()
    if user_key in accounts:
        return jsonify({"error": f"Username '{username}' is already registered. Please click Log In."}), 400

    pwd_hash, salt = hash_password(password)
    
    if not session_key:
        session_key = f"sf_live_{secrets.token_hex(16)}"

    accounts[user_key] = {
        "username": username,
        "password_hash": pwd_hash,
        "salt": salt,
        "session_key": session_key,
        "created_at": time.time(),
        "chats": []
    }
    save_accounts(accounts)

    return jsonify({
        "status": "ok",
        "user": {
            "username": username,
            "session_key": session_key,
            "chats": []
        }
    })

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    accounts = load_accounts()
    user_key = username.lower()
    user_data = accounts.get(user_key)

    if not user_data:
        return jsonify({"error": f"Account '{username}' does not exist. Click Register to create it!"}), 401

    if not verify_password(password, user_data["password_hash"], user_data["salt"]):
        return jsonify({"error": "Incorrect password. Please try again."}), 401

    return jsonify({
        "status": "ok",
        "user": {
            "username": user_data["username"],
            "session_key": user_data.get("session_key", f"sf_live_{secrets.token_hex(16)}"),
            "chats": user_data.get("chats", [])
        }
    })

@app.route("/api/auth/sync_chats", methods=["POST"])
def sync_chats_cloud():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    chats = data.get("chats", [])

    if not username:
        return jsonify({"status": "ignored"})

    accounts = load_accounts()
    user_key = username.lower()
    if user_key in accounts:
        accounts[user_key]["chats"] = chats
        save_accounts(accounts)
        return jsonify({"status": "synced"})

    return jsonify({"error": "User not found"}), 404

@app.route("/api/set_key", methods=["POST"])
def set_key():
    global openrouter_api_key
    data = request.json or {}
    openrouter_api_key = data.get("openrouter_key", "")
    return jsonify({"status": "ok"})

@app.route("/api/status", methods=["GET"])
def get_status():
    import time
    s_key = get_session_key(request)
    store = get_session_store(s_key)
    ctx = store["game_context"]
    is_connected = ctx.get("last_seen") and (time.time() - ctx["last_seen"] < 10)
    ctx["connected"] = is_connected
    return jsonify(ctx)

@app.route("/api/get_game_name/<place_id>", methods=["GET"])
def get_game_name_route(place_id):
    name = fetch_game_name(place_id)
    return jsonify({"place_id": place_id, "game_name": name})

@app.route("/api/generate_title", methods=["POST"])
def generate_title_route():
    data = request.json or {}
    prompt = data.get("prompt", "")
    if not prompt:
        return jsonify({"title": "New chat"})
    
    title_prompt = f"Summarize this user request into a short 2 to 4 word chat title. Return ONLY the title text without quotes, punctuation, or extra markdown:\n'{prompt}'"
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "") or openrouter_api_key
    reply, err = generate_ai_response(title_prompt, "openrouter/anthropic/claude-3.5-sonnet", [], game_ctx={}, openrouter_key=openrouter_key, ai_mode="chat")
    
    if reply:
        clean_title = reply.strip().strip('"').strip("'").split("\n")[0]
        clean_title = re.sub(r'^[#*\s-]+', '', clean_title)
        if len(clean_title) > 30:
            clean_title = clean_title[:30] + "..."
        return jsonify({"title": clean_title})
    
    fallback_title = prompt[:22] + "..." if len(prompt) > 22 else prompt
    return jsonify({"title": fallback_title})

@app.route("/api/context", methods=["POST"])
def update_context():
    import time
    data = request.json or {}
    s_key = get_session_key(request)
    store = get_session_store(s_key)
    
    store["game_context"].update({
        "connected": True,
        "last_seen": time.time(),
        "place_id": data.get("place_id", 0),
        "player_name": data.get("player_name", "Unknown"),
        "remotes": data.get("remotes", []),
        "leaderstats": data.get("leaderstats", {}),
        "workspace_items": data.get("workspace_items", [])
    })
    return jsonify({"status": "synced"})

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    s_key = get_session_key(request)
    store = get_session_store(s_key)

    prompt = data.get("prompt", "")
    openrouter_key = data.get("openrouter_key") or os.getenv("OPENROUTER_API_KEY", "") or openrouter_api_key
    selected_model = data.get("model", "openrouter/anthropic/claude-3.5-sonnet")
    ai_mode = data.get("ai_mode", "coding")
    
    auto_execute = data.get("auto_execute", False)
    auto_fix = data.get("auto_fix", True)
    history = data.get("history", [])

    reply, err = generate_ai_response(prompt, selected_model, history, store["game_context"], openrouter_key=openrouter_key, ai_mode=ai_mode)
    
    if err:
        return jsonify({"error": f"AI Generation Error: {err}"}), 500

    extracted_code = extract_luau_code(reply)
    
    script_id = None
    status = "completed"
    trajectory = []

    # Check executor connection state
    ctx = store.get("game_context", {})
    is_executor_connected = ctx.get("connected", False) and (time.time() - ctx.get("last_seen", 0) < 10)

    # ONLY queue and test scripts if ALL conditions are met:
    # 1. Mode is "coding"
    # 2. Executor is currently connected
    # 3. Valid Luau code was extracted
    if extracted_code and ai_mode == "coding" and is_executor_connected:
        script_id = f"scr_{int(time.time()*1000)}"
        
        trajectory = [
            "Initial script generated.",
            "Sent script to Roblox Executor for verification..."
        ]

        testing_reply = f"Testing and verifying script in Roblox Session..."

        store["script_sessions"][script_id] = {
            "status": "verifying",
            "attempts": 0,
            "auto_fix": auto_fix,
            "logs": trajectory,
            "original_reply": reply,
            "final_code": extracted_code,
            "reply": testing_reply,
            "history": history
        }

        store["pending_scripts"].append({"id": script_id, "code": extracted_code})
        status = "verifying"

        return jsonify({
            "reply": testing_reply,
            "script_id": script_id,
            "status": status,
            "trajectory": trajectory
        })

    # In Thinking mode, Chat mode, or when disconnected: simply display the response without executor execution
    return jsonify({
        "reply": reply,
        "script_id": None,
        "status": "completed",
        "trajectory": []
    })

@app.route("/api/script_status/<script_id>", methods=["GET"])
def get_script_status(script_id):
    s_key = get_session_key(request)
    store = get_session_store(s_key)
    sess = store["script_sessions"].get(script_id)
    if sess:
        return jsonify(sess)
    return jsonify({"error": "Script session not found"}), 404

@app.route("/api/report_success", methods=["POST"])
def report_success():
    data = request.json or {}
    s_key = get_session_key(request)
    store = get_session_store(s_key)
    script_id = data.get("script_id")
    code = data.get("code", "")

    sess = store["script_sessions"].get(script_id)
    if sess:
        sess["logs"].append("Verification Passed: Script executed with 0 errors in game!")
        sess["status"] = "verified"
        sess["final_code"] = code
        sess["reply"] = f"Script Verified (0 Errors)\n```luau\n{code}\n```"
        print(f"[VERIFIED SUCCESS] Session [{s_key}] Script [{script_id}] passed test with 0 errors!")

    return jsonify({"status": "acknowledged"})

@app.route("/api/report_error", methods=["POST"])
def report_error():
    data = request.json or {}
    s_key = get_session_key(request)
    store = get_session_store(s_key)

    script_id = data.get("script_id")
    failed_code = data.get("failed_code", "")
    error_msg = data.get("error_message", "Unknown runtime error")

    sess = store["script_sessions"].get(script_id)
    if not sess:
        sess = {
            "status": "verifying",
            "attempts": 0,
            "auto_fix": True,
            "logs": [],
            "reply": "",
            "history": []
        }
        store["script_sessions"][script_id] = sess

    attempts = sess.get("attempts", 0) + 1
    sess["attempts"] = attempts
    sess["logs"].append(f"Error Caught: {error_msg}")

    if attempts >= 3 or not sess.get("auto_fix", True):
        sess["logs"].append("Max auto-fix attempts reached (3/3). Stopping loop.")
        sess["status"] = "failed"
        sess["reply"] = f"Script Auto-Fix Limit Reached (Error: {error_msg})\n```luau\n{failed_code}\n```"
        return jsonify({"status": "max_attempts_reached"}), 400

    sess["logs"].append(f"Auto-Fixing Error... (Attempt {attempts}/3)...")

    debug_prompt = f"""
    The previous Luau script failed in the Roblox engine with the following error:

    [ROBLOX ENGINE ERROR LOG]
    {error_msg}

    [FAILED SCRIPT CODE]
    ```luau
    {failed_code}
    ```

    Please fix all errors in this code and return the corrected script inside a ```luau ... ``` code block.
    """

    openrouter_key = os.getenv("OPENROUTER_API_KEY", "") or openrouter_api_key
    selected_model = "openrouter/anthropic/claude-3.5-sonnet"

    reply, err = generate_ai_response(debug_prompt, selected_model, sess.get("history", []), store["game_context"], openrouter_key=openrouter_key, ai_mode="coding")

    if reply:
        fixed_code = extract_luau_code(reply)
        if fixed_code:
            sess["logs"].append(f"Queued fixed script (Attempt {attempts}/3) for verification...")
            store["pending_scripts"].append({"id": script_id, "code": fixed_code})
            return jsonify({"status": "auto_fixed", "new_code": fixed_code, "attempt": attempts})

    sess["status"] = "failed"
    sess["logs"].append("AI auto-fix failed to produce a valid solution.")
    return jsonify({"error": "Auto-fix failed"}), 500

@app.route("/api/get_autofix_logs", methods=["GET"])
def get_autofix_logs():
    return jsonify({"logs": []})

@app.route("/api/queue_script", methods=["POST"])
def queue_script():
    data = request.json or {}
    s_key = get_session_key(request)
    store = get_session_store(s_key)

    code = data.get("code")
    if code:
        script_id = f"manual_{int(time.time()*1000)}"
        store["pending_scripts"].append({"id": script_id, "code": code})
        return jsonify({"status": "queued"})
    return jsonify({"error": "No code provided"}), 400

@app.route("/api/pending_script", methods=["GET"])
def get_pending_script():
    s_key = get_session_key(request)
    store = get_session_store(s_key)

    if store["pending_scripts"]:
        item = store["pending_scripts"].pop(0)
        return jsonify({"has_script": True, "script_id": item["id"], "code": item["code"]})
    return jsonify({"has_script": False, "code": "", "script_id": ""})

if __name__ == "__main__":
    print("="*60)
    print(" ScriptForge Roblox Chat Server running!")
    print(" Open Web Chat in Browser: http://localhost:5000")
    print("="*60)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)

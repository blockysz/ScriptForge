import os
import sys
import json
import re
import time
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

# Global Multi-Tenant State
# Format: sessions_data[session_key] = { game_context, pending_scripts, script_sessions }
sessions_data = {}
game_name_cache = {}

DEFAULT_API_KEY = "ollama"
gemini_api_key = os.getenv("GEMINI_API_KEY", DEFAULT_API_KEY)
current_api_key = DEFAULT_API_KEY
current_selected_model = "ollama/qwen2.5-coder:latest"

@app.errorhandler(Exception)
def handle_exception(e):
    """Ensure all server errors return clean JSON instead of HTML pages."""
    return jsonify({"error": str(e)}), 500

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

def call_openai_compatible(api_url, api_key, model_names, system_instruction, user_prompt, history=[], game_ctx={}):
    """Call OpenAI compatible endpoints (OpenRouter / Ollama) with multi-model fallback."""
    if isinstance(model_names, str):
        models_to_try = [model_names]
    else:
        models_to_try = list(model_names)

    messages = [{"role": "system", "content": f"{system_instruction}\n\n[LIVE ROBLOX GAME CONTEXT]\n{json.dumps(game_ctx, indent=2)}"}]
    for item in history:
        r = item.get("role", "user")
        role = "assistant" if r in ["ai", "assistant", "model"] else "user"
        messages.append({"role": role, "content": item.get("content", "")})
    messages.append({"role": "user", "content": user_prompt})

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    if api_key and api_key != "ollama":
        headers["Authorization"] = f"Bearer {api_key}"

    last_err = None
    for model_name in models_to_try:
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
                print(f"[SUCCESS] Model used: {model_name}")
                return content, None
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            last_err = f"HTTP {e.code}: {err_body}"
            print(f"[WARNING] Model {model_name} failed ({e.code}), trying next model...")
        except Exception as e:
            last_err = str(e)

    return None, last_err

def call_gemini_api(api_key, system_instruction, user_prompt, history=[], target_model="gemini-3.6-flash", game_ctx={}):
    """Call Google Gemini REST API with correct role mapping."""
    contents = []
    full_system = f"{system_instruction}\n\n[LIVE ROBLOX GAME CONTEXT]\n{json.dumps(game_ctx, indent=2)}"
    
    for item in history:
        raw_role = item.get("role", "user")
        g_role = "model" if raw_role in ["ai", "assistant", "model"] else "user"
        contents.append({
            "role": g_role,
            "parts": [{"text": item.get("content", "")}]
        })
        
    contents.append({
        "role": "user",
        "parts": [{"text": f"[System Context: {full_system}]\n\nUser Question: {user_prompt}"}]
    })

    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192
        }
    }
    
    models_to_try = [target_model, "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.7-flash"]
    seen = set()
    models_to_try = [x for x in models_to_try if not (x in seen or seen.add(x))]

    last_err = None
    for model in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                candidates = res_data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    text = ""
                    for p in parts:
                        if "text" in p and not p.get("thought", False):
                            text += p["text"]
                    if not text and parts:
                        text = parts[-1].get("text", "")
                    if text:
                        print(f"[SUCCESS] Used Gemini model: {model}")
                        return text, None
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8")
            last_err = f"HTTP {e.code}: {err_body}"
            print(f"[WARNING] Model {model} failed ({e.code}): {err_body[:100]}")
        except Exception as e:
            last_err = str(e)

    return None, last_err

def generate_ai_response(user_prompt, selected_model, req_key, history=[], game_ctx={}):
    """UNIFIED AI generation engine used for BOTH chat messages and auto-fixes."""
    system_instruction = """
    You are ScriptForge's expert Luau Scripting Assistant connected directly to a live Roblox game player session.
    
    Your goals:
    1. Answer the user's questions clearly in normal Markdown chat.
    2. When requested to write or fix a script, generate valid Luau code inside ```luau ... ``` or ```lua ... ``` blocks.
    3. Use exact Remote names, paths, and workspace items provided in the live context.
    4. Write code suitable for a Roblox Client Executor (using standard Luau + executor tools like request, firetouchinterest, etc. if appropriate).
    """

    print(f"[AI PIPELINE] Generating response using model: {selected_model}")

    if selected_model.startswith("ollama/") or "qwen" in selected_model or req_key == "ollama":
        model_name = selected_model.replace("ollama/", "")
        return call_openai_compatible("http://localhost:11434/v1/chat/completions", "", model_name, system_instruction, user_prompt, history, game_ctx)
    elif selected_model.startswith("openrouter/"):
        m_name = selected_model.replace("openrouter/", "")
        openrouter_models = [m_name, "dots-studio/dots-3-note-preview:free", "cohere/north-mini-code:free", "google/gemma-4-31b-it:free"]
        return call_openai_compatible("https://openrouter.ai/api/v1/chat/completions", req_key, openrouter_models, system_instruction, user_prompt, history, game_ctx)
    else:
        g_model = selected_model.replace("gemini/", "")
        return call_gemini_api(req_key, system_instruction, user_prompt, history, target_model=g_model, game_ctx=game_ctx)

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ScriptForge | Live Roblox AI Studio</title>
    
    <!-- Custom SVG Favicon Icon for browser tab -->
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='20' fill='%23121212'/><path d='M25 65 L75 65 L70 50 L30 50 Z M40 50 L40 35 L60 35 L60 50 Z' fill='%23ffffff'/><polygon points='52 20 38 48 50 48 44 75 62 42 50 42' fill='%23ffffff'/></svg>">
    
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.7.0/styles/github-dark.min.css" id="highlightTheme">
    <style>
        :root {
            --bg-main: #121212;
            --bg-sidebar: #1a1a1a;
            --bg-card: #262626;
            --border-color: #333333;
            --text-primary: #f5f5f5;
            --text-secondary: #aaaaaa;
            --accent: #ffffff;
            --accent-hover: #e0e0e0;
            --btn-text: #000000;
            --input-bg: #222222;
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
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
            padding: 12px 24px;
            border-radius: 30px;
            font-size: 0.88rem;
            font-weight: 600;
            z-index: 9999;
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
            background-color: rgba(255, 255, 255, 0.1);
            color: var(--text-primary);
        }

        .status-offline {
            background-color: rgba(120, 120, 120, 0.1);
            color: var(--text-secondary);
        }

        .dot {
            width: 7px;
            height: 7px;
            border-radius: 50%;
        }
        .dot-online { background-color: #ffffff; box-shadow: 0 0 6px #ffffff; }
        .dot-offline { background-color: #777777; }

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
            overflow: hidden;
            flex-shrink: 0;
            display: flex;
            flex-direction: column;
            gap: 10px;
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
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
            transition: border-color 0.2s;
            width: 100%;
            box-sizing: border-box;
        }

        .input-box:focus-within {
            border-color: var(--text-primary);
        }

        .input-box textarea {
            flex: 1;
            background: transparent;
            border: none;
            color: var(--text-primary);
            resize: none;
            height: 24px;
            max-height: 120px;
            outline: none;
            font-size: 0.95rem;
            line-height: 1.4;
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
        }

        .toggle-group {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .custom-toggle-pill {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background-color: #222222;
            border: 1px solid #383838;
            padding: 5px 12px;
            border-radius: 20px;
            cursor: pointer;
            font-size: 0.8rem;
            font-weight: 600;
            color: #888888;
            transition: all 0.2s ease-in-out;
            user-select: none;
        }

        .custom-toggle-pill:hover {
            border-color: #666666;
            color: #cccccc;
        }

        .custom-toggle-pill.active {
            background-color: #333333;
            border-color: #666666;
            color: #ffffff;
        }

        .toggle-knob {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background-color: #555555;
            transition: all 0.2s ease-in-out;
            display: inline-block;
        }

        .custom-toggle-pill.active .toggle-knob {
            background-color: #ffffff;
            box-shadow: 0 0 5px rgba(255, 255, 255, 0.5);
        }

        .code-container {
            margin: 12px 0;
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid var(--border-color);
            max-width: 100%;
        }

        .code-header {
            background: #1a1a1a;
            padding: 6px 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.78rem;
            color: #aaaaaa;
            border-bottom: 1px solid #2a2a2a;
        }

        pre {
            margin: 0;
            padding: 14px;
            background: #0d0d0d !important;
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
            <button class="btn btn-sm btn-outline-light fw-bold" onclick="openExecutorModal()" title="Get Executor Client Script">
                <i class="fa-solid fa-plug me-1"></i> Connect Executor
            </button>

            <div id="statusBadge" class="status-badge status-offline">
                <span class="dot dot-offline"></span> Disconnected
            </div>

            <button class="btn btn-sm btn-outline-secondary border-0 text-light" onclick="toggleTheme()" id="themeBtn" title="Toggle Light / Dark Mode">
                <i class="fa-solid fa-moon"></i>
            </button>

            <button class="btn btn-sm btn-outline-secondary border-0 text-light" data-bs-toggle="modal" data-bs-target="#settingsModal" title="AI Settings">
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

                <div class="input-group input-group-sm mb-2">
                    <span class="input-group-text bg-black text-secondary border-secondary"><i class="fa-solid fa-magnifying-glass"></i></span>
                    <input type="text" id="chatSearchInput" class="form-control bg-black text-light border-secondary" placeholder="Search chats & messages..." oninput="renderChatList()">
                </div>

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
                    <button class="send-btn" id="sendBtn" onclick="sendMessage()"><i class="fa-solid fa-arrow-up"></i></button>
                </div>

                <div class="bottom-toggles-bar">
                    <div class="toggle-group">
                        <div class="custom-toggle-pill" id="autoExecPill" onclick="toggleAutoExecute()">
                            <span class="toggle-knob"></span>
                            <span><i class="fa-solid fa-bolt me-1"></i> Auto-Run Scripts</span>
                        </div>

                        <div class="custom-toggle-pill active" id="autoFixPill" onclick="toggleAutoFix()">
                            <span class="toggle-knob"></span>
                            <span><i class="fa-solid fa-wrench me-1"></i> Auto-Fix Errors</span>
                        </div>
                    </div>

                    <div class="text-secondary" style="font-size: 0.78rem;">
                        <i class="fa-solid fa-brain me-1"></i> Model: <strong class="text-light" id="bottomModelBadge">qwen2.5-coder</strong>
                    </div>
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

    <div class="modal fade" id="settingsModal" tabindex="-1">
        <div class="modal-dialog">
            <div class="modal-content bg-dark text-light border-secondary">
                <div class="modal-header border-secondary">
                    <h5 class="modal-title"><i class="fa-solid fa-sliders me-2"></i>AI Settings</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    <div class="mb-4">
                        <label class="form-label font-weight-bold text-light"><i class="fa-solid fa-brain me-1"></i> Choose AI Model</label>
                        <select id="modalModelSelector" class="form-select bg-secondary text-light border-0">
                            <option value="ollama/qwen2.5-coder:latest">🦙 Local Ollama: qwen2.5-coder (Free - Offline Default)</option>
                            <option value="openrouter/cohere/north-mini-code:free">🌐 OpenRouter Free: Cohere North Code</option>
                            <option value="openrouter/google/gemma-4-31b-it:free">🌐 OpenRouter Free: Google Gemma 4</option>
                            <option value="gemini/gemini-3.6-flash">✨ Google Gemini 3.6 Flash</option>
                            <option value="gemini/gemini-3.5-flash">✨ Google Gemini 3.5 Flash</option>
                        </select>
                    </div>

                    <div class="mb-3">
                        <label class="form-label font-weight-bold text-light"><i class="fa-solid fa-key me-1"></i> API Key (OpenRouter / Gemini)</label>
                        <input type="password" id="apiKeyInput" class="form-control bg-secondary text-light border-0" placeholder="ollama / sk-or-... / AIzaSy...">
                    </div>
                </div>
                <div class="modal-footer border-secondary">
                    <button type="button" class="btn btn-light px-4 fw-bold text-dark" onclick="saveSettings()">Save Settings</button>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.7.0/highlight.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/4.3.0/marked.min.js"></script>
    
    <script>
        // High-Entropy Player-Specific Session Key Generation
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

        let currentApiKey = localStorage.getItem("GEMINI_API_KEY") || "ollama";
        let selectedModel = localStorage.getItem("ANTIGRAVITY_SELECTED_MODEL") || "ollama/qwen2.5-coder:latest";
        let autoExecute = localStorage.getItem("ANTIGRAVITY_AUTO_EXECUTE") === "true";
        let autoFix = localStorage.getItem("ANTIGRAVITY_AUTO_FIX") !== "false";
        let currentTheme = localStorage.getItem("ANTIGRAVITY_THEME") || "dark";
        let wasConnected = false;
        let currentGameName = "General Roblox";
        let currentPlaceId = 0;
        let editingChatId = null;
        let activePollingScriptIds = {};

        document.documentElement.setAttribute("data-bs-theme", currentTheme);
        updateThemeIcon();

        document.getElementById("apiKeyInput").value = currentApiKey;
        document.getElementById("modalModelSelector").value = selectedModel;
        document.getElementById("bottomModelBadge").innerText = selectedModel.split('/')[1] || selectedModel;
        
        updateTogglePillsUI();

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
            showToast(autoExecute ? "⚡ Auto-Run Enabled!" : "Auto-Run Disabled", autoExecute ? "fa-solid fa-bolt text-light" : "fa-solid fa-circle-info text-secondary");
        }

        function toggleAutoFix() {
            autoFix = !autoFix;
            localStorage.setItem("ANTIGRAVITY_AUTO_FIX", autoFix);
            updateTogglePillsUI();
            showToast(autoFix ? "🔧 Auto-Fix Errors Active!" : "Auto-Fix Disabled", autoFix ? "fa-solid fa-wrench text-light" : "fa-solid fa-circle-info text-secondary");
        }

        function autoGrow(element) {
            element.style.height = "24px";
            element.style.height = Math.min(element.scrollHeight, 120) + "px";
        }

        function saveSettings() {
            const key = document.getElementById("apiKeyInput").value.trim();
            selectedModel = document.getElementById("modalModelSelector").value;

            localStorage.setItem("GEMINI_API_KEY", key);
            localStorage.setItem("ANTIGRAVITY_SELECTED_MODEL", selectedModel);
            currentApiKey = key;

            document.getElementById("bottomModelBadge").innerText = selectedModel.split('/')[1] || selectedModel;

            fetch('/api/set_key', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({api_key: key})
            });

            bootstrap.Modal.getInstance(document.getElementById('settingsModal')).hide();
            showToast("Settings Saved!", "fa-solid fa-circle-check text-light");
        }

        // Generate Formatted Multi-Line GitHub loadstring for current domain + unique Session Key
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
            showToast("📋 Session Loadstring copied to clipboard!", "fa-solid fa-copy text-light");
        }

        // Local Chat Storage Management
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
                        content: "👋 **Welcome to ScriptForge!** Connect your Roblox executor script to start writing & auto-fixing game scripts in real-time."
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
            select.innerHTML = '<option value="all">🎮 All Games</option>';
            
            const set = new Set();
            chats.forEach(c => {
                if (c.game_name) set.add(c.game_name);
            });

            set.forEach(gName => {
                const opt = document.createElement("option");
                opt.value = gName;
                opt.innerText = "🎮 " + gName;
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
                div1.innerHTML = '<i class="fa-solid fa-message me-1"></i> Matching Chats';
                listEl.appendChild(div1);

                titleMatches.forEach(chat => {
                    listEl.appendChild(createChatItemHTML(chat));
                });
            }

            if (messageMatches.length > 0) {
                const div2 = document.createElement("div");
                div2.className = "section-divider";
                div2.innerHTML = '<i class="fa-solid fa-magnifying-glass me-1"></i> Messages Found';
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
            
            let gameTag = chat.game_name ? `<span class="chat-item-sub"><i class="fa-solid fa-gamepad me-1"></i>${escapeHtml(chat.game_name)}</span>` : '';
            let snippetTag = snippet ? `<span class="chat-item-sub text-light"><i class="fa-solid fa-quote-left me-1"></i>${escapeHtml(snippet)}</span>` : '';

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
                        showToast(`🎮 Connected to ${data.player_name || "Roblox"}!`, "fa-solid fa-gamepad text-light");
                        
                        // Auto-close executor modal if open when connected
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
                        showToast("⚠️ Roblox Session Disconnected", "fa-solid fa-circle-exclamation text-secondary");
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

            // ChatGPT-Style AI Auto-Titling for new chats
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
                        <span>Forging code with ${escapeHtml(selectedModel.split('/')[1] || selectedModel)}...</span>
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
                        api_key: currentApiKey,
                        model: selectedModel,
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

        // Live Polling of Script Trajectory Logs
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
                    <span><i class="fa-solid fa-code me-1"></i> luau</span>
                    <div>
                        <button class="btn btn-sm btn-light me-1 text-dark py-0 px-2 fw-bold" style="font-size: 0.75rem;" onclick="runInRoblox(this)">
                            <i class="fa-solid fa-play me-1"></i> Run in Game
                        </button>
                        <button class="btn btn-sm btn-outline-secondary py-0 px-2 text-light" style="font-size: 0.75rem;" onclick="copyCode(this)">
                            <i class="fa-solid fa-copy me-1"></i> Copy
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
                showToast("🚀 Script queued to Roblox Executor!", "fa-solid fa-rocket text-light");
            } catch(e) {
                showToast("Error sending script: " + e.message, "fa-solid fa-circle-exclamation text-secondary");
            }
        }

        function copyCode(btn) {
            const code = btn.closest('.code-header').getAttribute('data-code');
            navigator.clipboard.writeText(code);
            showToast("📋 Code copied to clipboard!", "fa-solid fa-copy text-light");
        }

        function escapeHtml(str) {
            return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        }

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

@app.route("/api/set_key", methods=["POST"])
def set_key():
    global gemini_api_key, current_api_key
    data = request.json or {}
    gemini_api_key = data.get("api_key", DEFAULT_API_KEY)
    current_api_key = gemini_api_key
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
    req_key = gemini_api_key or DEFAULT_API_KEY
    reply, err = generate_ai_response(title_prompt, current_selected_model, req_key, [])
    
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
    global current_selected_model, current_api_key
    data = request.json or {}
    s_key = get_session_key(request)
    store = get_session_store(s_key)

    prompt = data.get("prompt", "")
    req_key = data.get("api_key") or gemini_api_key or DEFAULT_API_KEY
    selected_model = data.get("model", "ollama/qwen2.5-coder:latest")
    
    current_selected_model = selected_model
    current_api_key = req_key
    
    auto_execute = data.get("auto_execute", False)
    auto_fix = data.get("auto_fix", True)
    history = data.get("history", [])

    reply, err = generate_ai_response(prompt, selected_model, req_key, history, store["game_context"])
    
    if err:
        return jsonify({"error": f"AI Generation Error: {err}"}), 500

    extracted_code = extract_luau_code(reply)
    
    script_id = None
    status = "completed"
    trajectory = []

    if extracted_code:
        script_id = f"scr_{int(time.time()*1000)}"
        
        trajectory = [
            "🚀 Initial script generated.",
            "⏳ Sent script to Roblox Executor for verification..."
        ]

        testing_reply = f"⏳ **Testing and verifying script in Roblox Session...**"

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
    """Client reports that the script executed cleanly with 0 errors."""
    data = request.json or {}
    s_key = get_session_key(request)
    store = get_session_store(s_key)
    script_id = data.get("script_id")
    code = data.get("code", "")

    sess = store["script_sessions"].get(script_id)
    if sess:
        sess["logs"].append("✅ Verification Passed: Script executed with 0 errors in game!")
        sess["status"] = "verified"
        sess["final_code"] = code
        sess["reply"] = f"✅ **Script Verified (0 Errors)**\n```luau\n{code}\n```"
        print(f"[VERIFIED SUCCESS] Session [{s_key}] Script [{script_id}] passed test with 0 errors!")

    return jsonify({"status": "acknowledged"})

@app.route("/api/report_error", methods=["POST"])
def report_error():
    """Agentic Self-Healing Auto-Fix Endpoint."""
    global current_selected_model, current_api_key
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
    sess["logs"].append(f"❌ Error Caught: {error_msg}")

    if attempts >= 3 or not sess.get("auto_fix", True):
        sess["logs"].append("⚠️ Max auto-fix attempts reached (3/3). Stopping loop.")
        sess["status"] = "failed"
        sess["reply"] = f"⚠️ **Script Auto-Fix Limit Reached** (Error: {error_msg})\n```luau\n{failed_code}\n```"
        print(f"[AUTO-FIX STOP] Reached max retries for script [{script_id}].")
        return jsonify({"status": "max_attempts_reached"}), 400

    sess["logs"].append(f"🔧 Auto-Fixing Error... (Attempt {attempts}/3)...")
    print(f"[AUTO-FIX LOOP] Session [{s_key}] Script [{script_id}] failed! Attempt {attempts}/3. Sending error trace back to AI...")

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

    reply, err = generate_ai_response(debug_prompt, current_selected_model, current_api_key, sess.get("history", []), store["game_context"])

    if reply:
        fixed_code = extract_luau_code(reply)
        if fixed_code:
            sess["logs"].append(f"🚀 Queued fixed script (Attempt {attempts}/3) for verification...")
            store["pending_scripts"].append({"id": script_id, "code": fixed_code})
            print(f"[AUTO-FIX RE-QUEUED] Script [{script_id}] re-queued.")
            return jsonify({"status": "auto_fixed", "new_code": fixed_code, "attempt": attempts})

    sess["status"] = "failed"
    sess["logs"].append("❌ AI auto-fix failed to produce a valid solution.")
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

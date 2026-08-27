# 🔨 ScriptForge | AI Luau Studio & Self-Healing Executor Engine

ScriptForge is a live AI-powered Luau scripting assistant, auto-debugging engine, and Web Chat interface built for Roblox creators and developers.

It syncs directly with live Roblox game sessions, inspects in-game Workspace objects and Remotes, writes tailored Luau scripts, and **automatically catches and repairs syntax/runtime errors** in real-time.

---

## ✨ Key Features

- 🎮 **Live Game Session Sync**: Auto-detects player stats, place IDs, official game names, workspace models, and `ReplicatedStorage` Remotes.
- 🔧 **Agentic Self-Healing Auto-Fix**: Automatically catches loadstring / pcall errors in Roblox, passes exact error traces back to AI, and re-queues fixed code until 0 errors are verified.
- 🖤 **ChatGPT Dark Monochrome Interface**: Sleek, high-contrast black & white design with borderless in-place chat title editing.
- 🔍 **Search & Game Filter**: Search through past chats and message snippets, and filter chats by game title (*Murder Mystery 2*, *Blox Fruits*, etc.).
- 🌐 **Multi-Model Support**: Offline local Ollama (`qwen2.5-coder`), OpenRouter Free Gateway, and Google Gemini Flash REST API integration.
- 🚀 **Vercel Ready**: Ready to deploy as a Vercel Python Serverless Application.

---

## ⚡ Quick Start (Local Setup)

1. **Clone the Repository**:
   ```bash
   git clone https://github.com/blockysz/ScriptForge.git
   cd ScriptForge
   ```

2. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Run the Backend Server**:
   ```bash
   python app.py
   ```

4. **Open in Browser**:
   Navigate to `http://localhost:5000` in any web browser.

5. **Connect Your Roblox Executor**:
   Copy and run `roblox_client.lua` inside your preferred Roblox client executor (Solara, Wave, MacSploit, Delta).

---

## ☁️ Vercel Deployment Guide

ScriptForge includes native Vercel Serverless Function routing via `vercel.json` and `api/index.py`.

1. Push your repository to GitHub: `blockysz/ScriptForge`.
2. Go to [Vercel Dashboard](https://vercel.com/new).
3. Import the `blockysz/ScriptForge` repository.
4. Click **Deploy**!

---

## 📜 License

MIT License. Built for Roblox scripting automation & educational AI integration.

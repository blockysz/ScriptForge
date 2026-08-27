-- Antigravity Roblox Executor Client Script with Verification & Auto-Fix Feedback
-- Connects to local AI Chat Server running at http://localhost:5000

local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")
local LocalPlayer = Players.LocalPlayer

local req = (syn and syn.request) or (http and http.request) or request or http_request

if not req then
    error("[Antigravity] Your executor does not support HTTP requests (request/http_request required).")
end

local SERVER_URL = "http://127.0.0.1:5000"

print("==================================================")
print(" 🚀 Antigravity Executor Bridge Initializing...")
print(" 🔧 Verified Execution & Auto-Debug Loop ACTIVE")
print(" 🌐 Server URL: " .. SERVER_URL)
print("==================================================")

-- Function to collect game state & live context
local function gatherGameContext()
    local context = {
        place_id = game.PlaceId or 0,
        player_name = LocalPlayer and LocalPlayer.Name or "Unknown",
        remotes = {},
        leaderstats = {},
        workspace_items = {}
    }

    -- Remotes
    pcall(function()
        local rep = game:GetService("ReplicatedStorage")
        if rep then
            for _, child in ipairs(rep:GetDescendants()) do
                if child:IsA("RemoteEvent") or child:IsA("RemoteFunction") then
                    table.insert(context.remotes, {
                        name = child.Name,
                        type = child.ClassName,
                        path = child:GetFullName()
                    })
                end
            end
        end
    end)

    -- Leaderstats
    pcall(function()
        if LocalPlayer then
            local stats = LocalPlayer:FindFirstChild("leaderstats")
            if stats then
                for _, stat in ipairs(stats:GetChildren()) do
                    context.leaderstats[stat.Name] = tostring(stat.Value)
                end
            end
        end
    end)

    -- Workspace Items (first 50)
    pcall(function()
        local count = 0
        for _, item in ipairs(workspace:GetChildren()) do
            if count >= 50 then break end
            if item:IsA("Model") or item:IsA("Tool") or item:IsA("Folder") then
                count = count + 1
                table.insert(context.workspace_items, {
                    name = item.Name,
                    class = item.ClassName
                })
            end
        end
    end)

    return context
end

-- Helper to report success back to server
local function reportSuccessToServer(scriptId, code)
    print("✅ [Antigravity] Script executed cleanly with 0 errors!")
    pcall(function()
        req({
            Url = SERVER_URL .. "/api/report_success",
            Method = "POST",
            Headers = {["Content-Type"] = "application/json"},
            Body = HttpService:JSONEncode({
                script_id = scriptId,
                code = code
            })
        })
    end)
end

-- Helper to report runtime errors back to server for AI Auto-Fix
local function reportErrorToServer(scriptId, code, errMsg)
    print("⚠️ [Auto-Debug] Script failed! Reporting error trace to AI for auto-fix...")
    print("❌ Error:", errMsg)
    pcall(function()
        req({
            Url = SERVER_URL .. "/api/report_error",
            Method = "POST",
            Headers = {["Content-Type"] = "application/json"},
            Body = HttpService:JSONEncode({
                script_id = scriptId,
                failed_code = code,
                error_message = tostring(errMsg)
            })
        })
    end)
end

-- Sync context loop (runs every 4 seconds)
task.spawn(function()
    while true do
        pcall(function()
            local ctx = gatherGameContext()
            req({
                Url = SERVER_URL .. "/api/context",
                Method = "POST",
                Headers = {["Content-Type"] = "application/json"},
                Body = HttpService:JSONEncode(ctx)
            })
        end)
        task.wait(4)
    end
end)

-- Script execution loop with Verification & Auto-Fix tracking
task.spawn(function()
    print("[Antigravity] Listening for scripts generated from Web Chat...")
    while true do
        pcall(function()
            local res = req({
                Url = SERVER_URL .. "/api/pending_script",
                Method = "GET"
            })
            
            if res and res.StatusCode == 200 then
                local data = HttpService:JSONDecode(res.Body)
                if data.has_script and data.code and #data.code > 0 then
                    local rawCode = data.code
                    local scriptId = data.script_id or ("scr_" .. tostring(os.time()))
                    
                    print("--------------------------------------------------")
                    print("⚡ [Antigravity] Running script [" .. scriptId .. "]...")
                    print("--------------------------------------------------")
                    
                    local func, compileErr = loadstring(rawCode)
                    if not func then
                        reportErrorToServer(scriptId, rawCode, "Syntax/Compile Error: " .. tostring(compileErr))
                    else
                        local success, runtimeErr = pcall(func)
                        if success then
                            reportSuccessToServer(scriptId, rawCode)
                        else
                            reportErrorToServer(scriptId, rawCode, "Runtime Error: " .. tostring(runtimeErr))
                        end
                    end
                end
            end
        end)
        task.wait(1)
    end
end)

-- ScriptForge Roblox Studio MCP Integration Plugin
-- Place this script in your Roblox Studio Plugins folder or run it in Studio Command Bar.
-- It connects Roblox Studio directly to ScriptForge MCP REST/JSON-RPC API!

local HttpService = game:GetService("HttpService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local Workspace = game:GetService("Workspace")

local SERVER_URL = "https://scriptforge.vercel.app" -- Or your custom domain / localhost:5000
local SESSION_KEY = "sf_studio_mcp_session"

print("==================================================")
print(" 🔨 ScriptForge Roblox Studio MCP Plugin Loaded!")
print(" MCP API Endpoint: " .. SERVER_URL .. "/api/mcp")
print("==================================================")

local function getRemotesList()
    local remotes = {}
    pcall(function()
        for _, obj in ipairs(ReplicatedStorage:GetDescendants()) do
            if obj:IsA("RemoteEvent") or obj:IsA("RemoteFunction") then
                table.insert(remotes, obj:GetFullName())
            end
        end
    end)
    return remotes
end

local function getWorkspaceItems()
    local workspaceItems = {}
    pcall(function()
        for _, item in ipairs(Workspace:GetChildren()) do
            if item:IsA("Model") or item:IsA("Part") or item:IsA("Folder") then
                table.insert(workspaceItems, item.Name)
            end
        end
    end)
    return workspaceItems
end

local function syncStudioToScriptForge()
    local ok, err = pcall(function()
        local payload = {
            session_key = SESSION_KEY,
            place_id = game.PlaceId,
            player_name = "Studio Developer",
            connected = true,
            remotes = getRemotesList(),
            workspace_items = getWorkspaceItems()
        }

        local jsonBody = HttpService:JSONEncode(payload)
        local response = HttpService:PostAsync(SERVER_URL .. "/api/mcp/studio_sync", jsonBody, Enum.HttpContentType.ApplicationJson, false)
        print("[ScriptForge MCP] Studio Context Synced successfully!")
    end)

    if not ok then
        warn("[ScriptForge MCP Sync Error] Make sure HttpService.HttpEnabled is set to true in Studio! Error: " .. tostring(err))
    end
end

-- Run initial sync & loop every 15 seconds
task.spawn(function()
    syncStudioToScriptForge()
    while task.wait(15) do
        syncStudioToScriptForge()
    end
end)

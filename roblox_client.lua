-- ScriptForge Roblox Executor Client
-- Hosted on GitHub: blockysz/ScriptForge

local SERVER_URL = getgenv().SCRIPTFORGE_URL or "http://localhost:5000"

local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local Workspace = game:GetService("Workspace")
local LocalPlayer = Players.LocalPlayer

print("==================================================")
print(" 🔨 ScriptForge AI Studio Client Loaded!")
print(" Target Domain: " .. SERVER_URL)
print("==================================================")

local requestFunc = (syn and syn.request) or (http and http.request) or http_request or (fluxus and fluxus.request) or request
if not requestFunc then
    error("[ScriptForge] Your executor does not support HTTP requests!")
end

local function getGameContext()
    local remotes = {}
    for _, obj in ipairs(ReplicatedStorage:GetDescendants()) do
        if obj:IsA("RemoteEvent") or obj:IsA("RemoteFunction") then
            table.insert(remotes, obj:GetFullName())
        end
    end

    local leaderstats = {}
    if LocalPlayer:FindFirstChild("leaderstats") then
        for _, stat in ipairs(LocalPlayer.leaderstats:GetChildren()) do
            leaderstats[stat.Name] = tostring(stat.Value)
        end
    end

    local workspaceItems = {}
    for _, item in ipairs(Workspace:GetChildren()) do
        if item:IsA("Model") or item:IsA("Part") or item:IsA("Folder") then
            table.insert(workspaceItems, item.Name)
        end
    end

    return {
        place_id = game.PlaceId,
        player_name = LocalPlayer.Name,
        remotes = remotes,
        leaderstats = leaderstats,
        workspace_items = workspaceItems
    }
end

local function syncContext()
    pcall(function()
        requestFunc({
            Url = SERVER_URL .. "/api/context",
            Method = "POST",
            Headers = {["Content-Type"] = "application/json"},
            Body = HttpService:JSONEncode(getGameContext())
        })
    end)
end

local function checkPendingScripts()
    local success, response = pcall(function()
        return requestFunc({
            Url = SERVER_URL .. "/api/pending_script",
            Method = "GET"
        })
    end)

    if success and response and response.StatusCode == 200 then
        local data = HttpService:JSONDecode(response.Body)
        if data and data.has_script and data.code and data.code ~= "" then
            print("[ScriptForge] 🚀 Executing script [" .. tostring(data.script_id) .. "]...")
            
            local func, compileErr = loadstring(data.code)
            if not func then
                requestFunc({
                    Url = SERVER_URL .. "/api/report_error",
                    Method = "POST",
                    Headers = {["Content-Type"] = "application/json"},
                    Body = HttpService:JSONEncode({
                        script_id = data.script_id,
                        failed_code = data.code,
                        error_message = "Syntax/Compile Error: " .. tostring(compileErr)
                    })
                })
            else
                local execSuccess, runtimeErr = xpcall(func, function(err)
                    return debug.traceback(tostring(err))
                end)

                if execSuccess then
                    requestFunc({
                        Url = SERVER_URL .. "/api/report_success",
                        Method = "POST",
                        Headers = {["Content-Type"] = "application/json"},
                        Body = HttpService:JSONEncode({
                            script_id = data.script_id,
                            code = data.code
                        })
                    })
                else
                    requestFunc({
                        Url = SERVER_URL .. "/api/report_error",
                        Method = "POST",
                        Headers = {["Content-Type"] = "application/json"},
                        Body = HttpService:JSONEncode({
                            script_id = data.script_id,
                            failed_code = data.code,
                            error_message = "Runtime Error: " .. tostring(runtimeErr)
                        })
                    })
                end
            end
        end
    end
end

task.spawn(function()
    while true do
        syncContext()
        checkPendingScripts()
        task.wait(1.5)
    end
end)

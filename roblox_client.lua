-- ScriptForge Roblox Executor Client
-- Hosted on GitHub: blockysz/ScriptForge

local SERVER_URL = (getgenv().SCRIPTFORGE_URL or "http://localhost:5000"):gsub("/+$", "")
local SESSION_KEY = getgenv().SESSION_KEY or "default_session"

local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local Workspace = game:GetService("Workspace")

local requestFunc = (syn and syn.request) or (http and http.request) or http_request or (fluxus and fluxus.request) or request
if not requestFunc then
    warn("[ScriptForge Error] Your executor does not support HTTP requests (syn.request / http_request / request)!")
    error("[ScriptForge] Executor HTTP requests unavailable!")
end

print("==================================================")
print(" 🔨 ScriptForge AI Studio Client Loaded!")
print(" Target Domain: " .. SERVER_URL)
print(" Session Token: " .. SESSION_KEY)
print("==================================================")

local lastFullSync = 0

local function getLocalPlayerName()
    local lp = Players.LocalPlayer
    if lp then return lp.Name end
    return "Player"
end

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

local function getLeaderstats()
    local leaderstats = {}
    pcall(function()
        local lp = Players.LocalPlayer
        if lp and lp:FindFirstChild("leaderstats") then
            for _, stat in ipairs(lp.leaderstats:GetChildren()) do
                leaderstats[stat.Name] = tostring(stat.Value)
            end
        end
    end)
    return leaderstats
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

local function syncContext()
    local ok, err = pcall(function()
        local now = tick()
        local isFull = (now - lastFullSync > 12)
        if isFull then
            lastFullSync = now
        end

        local payload = {
            session_key = SESSION_KEY,
            place_id = game.PlaceId,
            player_name = getLocalPlayerName(),
            connected = true
        }

        if isFull then
            payload.remotes = getRemotesList()
            payload.leaderstats = getLeaderstats()
            payload.workspace_items = getWorkspaceItems()
        end

        requestFunc({
            Url = SERVER_URL .. "/api/context",
            Method = "POST",
            Headers = {
                ["Content-Type"] = "application/json",
                ["X-Session-Key"] = SESSION_KEY
            },
            Body = HttpService:JSONEncode(payload)
        })
    end)

    if not ok then
        warn("[ScriptForge Sync Warning]: " .. tostring(err))
    end
end

local function isHttpOk(response)
    if not response then return false end
    local code = response.StatusCode or response.Status or response.status_code or response.status
    if not code then
        return response.Body ~= nil and response.Body ~= ""
    end
    if type(code) == "number" then
        return code == 200 or code == 0
    end
    if type(code) == "string" then
        return code:find("200") ~= nil or code == "OK"
    end
    return true
end

local function checkPendingScripts()
    local success, response = pcall(function()
        return requestFunc({
            Url = SERVER_URL .. "/api/pending_script?session_key=" .. HttpService:UrlEncode(SESSION_KEY),
            Method = "GET",
            Headers = {
                ["X-Session-Key"] = SESSION_KEY
            }
        })
    end)

    if success and response and isHttpOk(response) then
        local decodeOk, data = pcall(function()
            return HttpService:JSONDecode(response.Body)
        end)

        if decodeOk and data and data.has_script and data.code and data.code ~= "" then
            print("[ScriptForge] 🚀 Executing script [" .. tostring(data.script_id) .. "]...")
            
            local func, compileErr = loadstring(data.code)
            if not func then
                print("[ScriptForge Compile Error]: " .. tostring(compileErr))
                requestFunc({
                    Url = SERVER_URL .. "/api/report_error",
                    Method = "POST",
                    Headers = {
                        ["Content-Type"] = "application/json",
                        ["X-Session-Key"] = SESSION_KEY
                    },
                    Body = HttpService:JSONEncode({
                        session_key = SESSION_KEY,
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
                    print("[ScriptForge Execution Success] Script [" .. tostring(data.script_id) .. "] executed with 0 errors!")
                    requestFunc({
                        Url = SERVER_URL .. "/api/report_success",
                        Method = "POST",
                        Headers = {
                            ["Content-Type"] = "application/json",
                            ["X-Session-Key"] = SESSION_KEY
                        },
                        Body = HttpService:JSONEncode({
                            session_key = SESSION_KEY,
                            script_id = data.script_id,
                            code = data.code
                        })
                    })
                else
                    print("[ScriptForge Runtime Error]: " .. tostring(runtimeErr))
                    requestFunc({
                        Url = SERVER_URL .. "/api/report_error",
                        Method = "POST",
                        Headers = {
                            ["Content-Type"] = "application/json",
                            ["X-Session-Key"] = SESSION_KEY
                        },
                        Body = HttpService:JSONEncode({
                            session_key = SESSION_KEY,
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

-- Independent non-blocking loops for heartbeat and script polling
task.spawn(function()
    while true do
        syncContext()
        task.wait(2)
    end
end)

task.spawn(function()
    while true do
        checkPendingScripts()
        task.wait(1)
    end
end)

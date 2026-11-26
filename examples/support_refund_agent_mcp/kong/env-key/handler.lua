-- env-key: after the AgentDNA validation service said yes, put the app's key on the call.
--
-- The key is in an environment variable (kong.yml: key = "{vault://env/...}"),
-- so it never sits in a file, and agents never hold it. A call the validation service did
-- not check and allow gets no key, so the app refuses it. With mcp_session, the MCP
-- session's own messages, which run no tool, get it too.

local EnvKey = { PRIORITY = 850, VERSION = "0.1.0" }  -- right after agentdna-validation (900)

function EnvKey:access(conf)
  local allowed = kong.ctx.shared.agentdna  -- the validation service said yes
  -- mcp_session: an MCP server that wants the key on the whole session (initialize,
  -- tools/list, ...) gets it there too. Those run no tool; a tools/call still needs the yes.
  local session = conf.mcp_session and kong.ctx.shared.mcp_session_message
  if not (allowed or session) then
    return  -- no key
  end
  kong.service.request.set_header(conf.header, conf.key)
end

return EnvKey

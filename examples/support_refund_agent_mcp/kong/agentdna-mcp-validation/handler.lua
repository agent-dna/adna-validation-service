-- agentdna-mcp-validation: Kong asks the AgentDNA validation service about every
-- MCP tools/call that carries a workflow, then does what it says. All AgentDNA work
-- happens in the validation service; this only carries the answers in and out.
-- Any other MCP message (initialize, tools/list, ...) and a tools/call with no
-- workflow aren't AgentDNA's: they go on as they would without this plugin.
-- A body too big to read is refused (413).
--
-- For plugins after it, it marks the MCP session's own messages, the ones that
-- run no tool (kong.ctx.shared.mcp_session_message): a GET or DELETE, or one
-- JSON-RPC message that isn't a tools/call. A tools/call never is, signed or not.
--
-- Before forwarding (/mcp/check):
--   yes -> forward the tools/call the service returns (the agent's, without its workflow)
--   no  -> answer the caller with the service's status (the service already recorded it)
--   service unreachable or broken -> refuse (fail closed)
--
-- After the MCP server answers (/mcp/answered), whatever the answer:
--   result -> send the caller exactly the JSON-RPC answer the service returns: the
--             tool's result, signed onto the agent's call
--   error  -> the service records it; the MCP server's answer passes through
--   service unreachable -> the MCP server has already done the work, so its answer
--             passes through unsigned, and the agent's relay refuses it as unsigned.

local http = require "resty.http"
local cjson = require "cjson.safe"

local AgentDNAMCPValidation = { PRIORITY = 900, VERSION = "0.1.0" }

local function post(url, timeout, data)
  local client = http.new()
  client:set_timeout(timeout)
  local reply, err = client:request_uri(url, {
    method = "POST",
    body = cjson.encode(data),
    headers = { ["Content-Type"] = "application/json" },
  })
  if not reply or reply.status ~= 200 then
    return nil, err or ("status " .. reply.status)
  end
  return cjson.decode(reply.body)
end

-- A tools/call with a signed workflow in params._meta.agentdna
local function signed_tools_call(message)
  return type(message) == "table"
    and message.method == "tools/call"
    and type(message.params) == "table"
    and type(message.params._meta) == "table"
    and type(message.params._meta.agentdna) == "table"
end

-- One of the MCP session's own messages, which run no tool: a GET or DELETE (the
-- session's event stream, its end), or one JSON-RPC message that isn't a tools/call.
-- Not a batch: it could hold a tools/call.
local function session_message(message)
  local method = kong.request.get_method()
  if method == "GET" or method == "DELETE" then
    return true
  end
  return type(message) == "table"
    and message.jsonrpc == "2.0"
    and message[1] == nil
    and message.method ~= "tools/call"
end

function AgentDNAMCPValidation:access(conf)
  local body, err = kong.request.get_raw_body()
  if not body then  -- bigger than nginx_http_client_body_buffer_size
    return kong.response.exit(413, { error = "the call is too big for the gateway: " .. (err or "") })
  end

  local message = cjson.decode(body)
  if not signed_tools_call(message) then
    kong.ctx.shared.mcp_session_message = session_message(message)
    return  -- not AgentDNA's: the route's own settings decide
  end

  local verdict = post(conf.validation_url .. "/validation/v1/mcp/check", conf.timeout, {
    body = body,
    headers = kong.request.get_headers(),  -- what the caller sent: for the credential row (server_in)
  })
  if not verdict then
    return kong.response.exit(503, { error = "the AgentDNA validation service did not answer" })
  end
  if verdict.allowed ~= true then
    return kong.response.exit(verdict.status, { error = verdict.reason, code = verdict.code })
  end

  kong.ctx.plugin.workflow = verdict.workflow
  kong.ctx.shared.agentdna = { workflow = verdict.workflow, run_id = verdict.run_id }  -- for plugins after this one
  kong.service.request.enable_buffering()  -- hold the MCP server's answer, to hand it to the service
  kong.service.request.clear_header("Accept-Encoding")  -- the service signs the answer as text: no compression
  kong.service.request.set_raw_body(verdict.body)
end

function AgentDNAMCPValidation:response(conf)
  if not kong.ctx.plugin.workflow then
    return
  end

  local status = kong.service.response.get_status()
  local result, err = post(conf.validation_url .. "/validation/v1/mcp/answered", conf.timeout, {
    workflow = kong.ctx.plugin.workflow,
    status = status,
    body = kong.service.response.get_raw_body() or "",
    -- what Kong sent the MCP server, and where: for the credential row (server_out)
    headers = ngx.req.get_headers(),
    destination = ngx.var.upstream_scheme .. "://" .. ngx.var.upstream_host .. ngx.var.upstream_uri,
  })

  if result and result.body then
    return kong.response.exit(status, result.body, { ["Content-Type"] = "application/json" })
  elseif not result then
    kong.log.err("agentdna-mcp-validation: /mcp/answered failed, the answer passes through unsigned: ", err)
  end
end

return AgentDNAMCPValidation

return {
  name = "env-key",
  fields = {
    { config = {
        type = "record",
        fields = {
          -- The whole header value, e.g. "{vault://env/stripe-auth}" -> STRIPE_AUTH
          { key = { type = "string", required = true, referenceable = true } },
          { header = { type = "string", default = "Authorization" } },
          -- Also on the MCP session's own messages, which run no tool (initialize, tools/list, ...)
          { mcp_session = { type = "boolean", default = false } },
        },
    } },
  },
}

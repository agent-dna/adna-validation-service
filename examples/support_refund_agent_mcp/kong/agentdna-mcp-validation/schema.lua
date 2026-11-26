local typedefs = require "kong.db.schema.typedefs"

return {
  name = "agentdna-mcp-validation",
  fields = {
    { protocols = typedefs.protocols_http },
    { config = {
        type = "record",
        fields = {
          { validation_url = { type = "string", required = true } },   -- the validation service, e.g. http://127.0.0.1:8810
          { timeout = { type = "number", default = 10000 } },    -- ms; no answer in time = refused
        },
    } },
  },
}

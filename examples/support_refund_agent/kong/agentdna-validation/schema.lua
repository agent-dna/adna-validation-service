local typedefs = require "kong.db.schema.typedefs"

return {
  name = "agentdna-validation",
  fields = {
    { protocols = typedefs.protocols_http },
    { config = {
        type = "record",
        fields = {
          { validation_url = { type = "string", required = true } },   -- the validation service, e.g. http://host:8810
          { timeout = { type = "number", default = 10000 } },    -- ms; no answer in time = refused
        },
    } },
  },
}

#!/usr/bin/env bash
# Stop Kong, the validation service, the Stripe MCP server and both agents.

cd "$(dirname "$0")"
for port in 8810 8811 8812 8813; do
  pid=$(lsof -tiTCP:$port -sTCP:LISTEN 2> /dev/null)
  [ -n "$pid" ] && kill $pid
done
docker compose -p support-refund-agent-mcp --env-file .env -f kong/docker-compose.yml down > /dev/null 2>&1
echo "Stopped."

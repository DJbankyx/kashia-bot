#!/bin/bash
# Kashia Bot — Register the Telegram slash-command menu (setMyCommands).
#
# Populates the in-app "/" command menu (the list that pops up next to the
# message box). Run once, and again whenever COMMAND_MENU changes in
# src/handlers/telegram_webhook.py.
#
# Usage: ./set_telegram_commands.sh
# Requires: awscli (eu-west-1), python3 (for the venv), curl.

set -e
REGION="eu-west-1"

TOKEN=$(aws ssm get-parameter --region "$REGION" \
  --name "/kashia/telegram-bot-token" --with-decryption \
  --query "Parameter.Value" --output text 2>/dev/null || true)

if [ -z "$TOKEN" ] || [ "$TOKEN" = "None" ]; then
  echo "❌ /kashia/telegram-bot-token not found in SSM ($REGION)."
  exit 1
fi

# Pull the single source of truth (COMMAND_MENU) from the handler so this
# script and the bot never drift.
COMMANDS_JSON=$(cd "$(dirname "$0")" && ./venv/bin/python -c "
import sys, json
sys.path.insert(0, 'src')
from handlers.telegram_webhook import COMMAND_MENU
print(json.dumps(COMMAND_MENU))
")

echo "📋 Registering commands: $COMMANDS_JSON"

RESP=$(curl -s -X POST "https://api.telegram.org/bot${TOKEN}/setMyCommands" \
  -H "Content-Type: application/json" \
  -d "{\"commands\": ${COMMANDS_JSON}}")

echo "🤖 Telegram response: $RESP"
echo ""
echo "ℹ️  Current commands:"
curl -s "https://api.telegram.org/bot${TOKEN}/getMyCommands"
echo ""
echo "✅ Done. Open the bot — the '/' menu should list these commands."

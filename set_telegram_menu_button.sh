#!/bin/bash
# Kashia Bot — Set the Telegram Menu Button to open the Mini App (N6).
#
# The Menu Button is the persistent button bottom-left of the chat input. This
# points it at the deployed /app page (the Mini App). Run ONCE after the first
# deploy that includes MiniAppFunction, and again only if the API URL changes.
#
# Usage:
#   ./set_telegram_menu_button.sh [dev|prod]
#   Default stage: dev
#
# Reads the bot token from SSM and the Mini App URL from the stack outputs.
# Requires: awscli (eu-west-1), curl.

set -e

STAGE="${1:-dev}"
REGION="eu-west-1"
STACK_NAME="kashia-bot"

echo "🔘 Setting Telegram Menu Button (Mini App) for stage [$STAGE]..."

# ── 1. Bot token from SSM ──
TOKEN=$(aws ssm get-parameter --region "$REGION" \
  --name "/kashia/telegram-bot-token" --with-decryption \
  --query "Parameter.Value" --output text 2>/dev/null || true)
if [ -z "$TOKEN" ] || [ "$TOKEN" = "None" ]; then
  echo "❌ /kashia/telegram-bot-token not found in SSM ($REGION)."
  exit 1
fi

# ── 2. Mini App URL from stack outputs ──
URL=$(aws cloudformation describe-stacks --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='MiniAppUrl'].OutputValue" \
  --output text 2>/dev/null || true)
if [ -z "$URL" ] || [ "$URL" = "None" ]; then
  echo "❌ Could not read MiniAppUrl from stack '$STACK_NAME'."
  echo "   Deploy first (./deploy.sh $STAGE)."
  exit 1
fi

echo "🌐 Mini App URL: $URL"

# ── 3. Register the Menu Button (a web_app button) ──
# Telegram requires HTTPS (API Gateway URLs already are).
RESP=$(curl -s -X POST "https://api.telegram.org/bot${TOKEN}/setChatMenuButton" \
  -H "Content-Type: application/json" \
  -d "{\"menu_button\":{\"type\":\"web_app\",\"text\":\"📊 Dashboard\",\"web_app\":{\"url\":\"${URL}\"}}}")

echo "🤖 Telegram response: $RESP"
echo ""
echo "✅ Done. Open a chat with the bot — the bottom-left button now opens the Mini App."

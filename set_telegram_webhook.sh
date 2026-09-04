#!/bin/bash
# Kashia Bot — Register (or refresh) the Telegram webhook.
#
# Tells Telegram to POST updates to your deployed /telegram endpoint. Run this
# ONCE after the first deploy, and again if the API Gateway URL ever changes.
#
# Usage:
#   ./set_telegram_webhook.sh [dev|prod]
#   Default stage: dev
#
# It reads everything it needs automatically:
#   - bot token + webhook secret from SSM (/kashia/telegram-*)
#   - the webhook URL from the deployed CloudFormation stack outputs
#
# Requires: awscli configured for eu-west-1, jq, curl.

set -e

STAGE="${1:-dev}"
REGION="eu-west-1"
# Stack name comes from samconfig.toml ([default.deploy.parameters] stack_name).
# It is not stage-suffixed, so both dev/prod deploy to the same stack today.
STACK_NAME="kashia-bot"

echo "🔗 Configuring Telegram webhook for stage [$STAGE]..."

# ── 1. Pull the bot token + secret from SSM ──
TOKEN=$(aws ssm get-parameter --region "$REGION" \
  --name "/kashia/telegram-bot-token" --with-decryption \
  --query "Parameter.Value" --output text 2>/dev/null || true)

if [ -z "$TOKEN" ] || [ "$TOKEN" = "None" ]; then
  echo "❌ /kashia/telegram-bot-token not found in SSM ($REGION)."
  echo "   Create it first:"
  echo "   aws ssm put-parameter --region $REGION --name /kashia/telegram-bot-token --type SecureString --value <BOTFATHER_TOKEN>"
  exit 1
fi

SECRET=$(aws ssm get-parameter --region "$REGION" \
  --name "/kashia/telegram-webhook-secret" --with-decryption \
  --query "Parameter.Value" --output text 2>/dev/null || true)
if [ -z "$SECRET" ] || [ "$SECRET" = "None" ]; then
  echo "⚠️  No /kashia/telegram-webhook-secret set — registering without a secret token."
  SECRET=""
fi

# ── 2. Find the deployed webhook URL from stack outputs ──
URL=$(aws cloudformation describe-stacks --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='TelegramWebhookUrl'].OutputValue" \
  --output text 2>/dev/null || true)

if [ -z "$URL" ] || [ "$URL" = "None" ]; then
  echo "❌ Could not read TelegramWebhookUrl from stack '$STACK_NAME'."
  echo "   Deploy first (./deploy.sh $STAGE), or fix STACK_NAME in this script."
  exit 1
fi

echo "📡 Webhook URL: $URL"

# ── 3. Register with Telegram ──
# drop_pending_updates=true clears any backlog queued while the webhook was unset.
if [ -n "$SECRET" ]; then
  RESP=$(curl -s -X POST "https://api.telegram.org/bot${TOKEN}/setWebhook" \
    -d "url=${URL}" \
    -d "secret_token=${SECRET}" \
    -d "drop_pending_updates=true")
else
  RESP=$(curl -s -X POST "https://api.telegram.org/bot${TOKEN}/setWebhook" \
    -d "url=${URL}" \
    -d "drop_pending_updates=true")
fi

echo "🤖 Telegram response: $RESP"

# ── 4. Show the resulting webhook info for confirmation ──
echo ""
echo "ℹ️  Current webhook info:"
curl -s "https://api.telegram.org/bot${TOKEN}/getWebhookInfo"
echo ""
echo "✅ Done. Message your bot on Telegram (/start) to test."

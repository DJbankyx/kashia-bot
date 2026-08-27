#!/bin/bash
# Kashia Bot — One-command deploy
# Usage: ./deploy.sh [dev|prod]
# Default: dev

set -e

STAGE="${1:-dev}"

echo "🚀 Deploying Kashia Bot to [$STAGE]..."

# Step 1: Stamp the build timestamp into template.yaml description
TIMESTAMP=$(date +%Y%m%d%H%M%S)
sed -i "s/Description: Kashia WhatsApp Bot.*/Description: Kashia WhatsApp Bot - Build ${TIMESTAMP}/" template.yaml
echo "📋 Build timestamp: ${TIMESTAMP}"

# Step 2: Build
echo "🔨 Building..."
rm -rf .aws-sam/build
sam build

# Step 3: Deploy
# NOTE: --force-upload removed. It re-uploaded the full ~14.5MB dependency
# layer on every deploy (slow and stall-prone). Without it, SAM skips
# artifacts whose contents haven't changed. If you ever need to force a
# clean re-upload, run: ./deploy.sh dev --force  (see below).
echo "☁️  Deploying to AWS (${STAGE})..."

FORCE_FLAG=""
if [ "$2" = "--force" ]; then
  FORCE_FLAG="--force-upload"
  echo "⚠️  Forcing full artifact re-upload."
fi

sam deploy \
  --no-confirm-changeset \
  ${FORCE_FLAG} \
  --parameter-overrides "Stage=${STAGE}"

echo ""
echo "✅ Deploy complete! Build: ${TIMESTAMP}"
echo "📡 Check the Outputs above for your webhook URL."

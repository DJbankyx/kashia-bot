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
echo "☁️  Deploying to AWS (${STAGE})..."
sam deploy \
  --no-confirm-changeset \
  --force-upload \
  --parameter-overrides "Stage=${STAGE}"

echo ""
echo "✅ Deploy complete! Build: ${TIMESTAMP}"
echo "📡 Check the Outputs above for your webhook URL."

#!/bin/bash
# Kashia Bot — One-command deploy
# Usage: ./deploy.sh

set -e

echo "🚀 Deploying Kashia Bot..."

# Step 1: Copy files from Windows to WSL
echo "📋 Copying files..."
cp /mnt/c/Users/HP/Desktop/projects/kashia-bot/src/services/conversation_engine.py ~/projects/kashia-bot/src/services/
cp /mnt/c/Users/HP/Desktop/projects/kashia-bot/src/services/export_service.py ~/projects/kashia-bot/src/services/
cp /mnt/c/Users/HP/Desktop/projects/kashia-bot/src/services/pdf_generator.py ~/projects/kashia-bot/src/services/
cp /mnt/c/Users/HP/Desktop/projects/kashia-bot/src/services/categorizer.py ~/projects/kashia-bot/src/services/
cp /mnt/c/Users/HP/Desktop/projects/kashia-bot/src/services/database.py ~/projects/kashia-bot/src/services/
cp /mnt/c/Users/HP/Desktop/projects/kashia-bot/src/utils/parser.py ~/projects/kashia-bot/src/utils/
cp /mnt/c/Users/HP/Desktop/projects/kashia-bot/src/main.py ~/projects/kashia-bot/src/

# Step 2: Force template change (timestamp in Description)
TIMESTAMP=$(date +%Y%m%d%H%M%S)
sed -i "s/Description:.*/Description: Kashia WhatsApp Bot - Build ${TIMESTAMP}/" template.yaml

# Step 3: Build and deploy
echo "🔨 Building..."
rm -rf .aws-sam/build
sam build

echo "☁️ Deploying..."
sam deploy --no-confirm-changeset --force-upload

echo "✅ Deploy complete! Build: ${TIMESTAMP}"

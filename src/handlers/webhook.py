# src/handlers/webhook.py
"""WhatsApp Webhook Handler - receives and processes incoming messages"""

import json
import hashlib
import hmac
import logging

# Set up logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """
    Main entry point for the Lambda function.
    Handles both GET (webhook verification) and POST (incoming messages).
    """
    http_method = event.get('httpMethod', '')

    if http_method == 'GET':
        return handle_verification(event)
    elif http_method == 'POST':
        return handle_incoming_message(event)
    else:
        return response(405, {'error': 'Method not allowed'})


def handle_verification(event):
    """
    WhatsApp webhook verification (GET request).
    Meta sends a challenge token — we must echo it back to prove we own the URL.
    """
    params = event.get('queryStringParameters', {}) or {}

    mode = params.get('hub.mode', '')
    token = params.get('hub.verify_token', '')
    challenge = params.get('hub.challenge', '')

    # Import here to avoid loading SSM on every cold start
    from utils.config import get_verify_token

    if mode == 'subscribe' and token == get_verify_token():
        logger.info('Webhook verified successfully!')
        # Must return the challenge as plain text (not JSON)
        return {
            'statusCode': 200,
            'body': challenge,
            'headers': {'Content-Type': 'text/plain'}
        }
    else:
        logger.warning(f'Webhook verification failed. Mode: {mode}, Token: {token}')
        return response(403, {'error': 'Verification failed'})


def handle_incoming_message(event):
    """
    Process incoming WhatsApp message (POST request).
    Parses the message, extracts text, and will route to conversation engine.
    """
    try:
        # Verify request is from Meta
        if not verify_signature(event):
            logger.warning('Invalid webhook signature — rejecting request')
            return response(401, {'error': 'Invalid signature'})

        body = json.loads(event.get('body', '{}'))

        # WhatsApp sends many types of notifications — we only care about messages
        entries = body.get('entry', [])

        for entry in entries:
            changes = entry.get('changes', [])

            for change in changes:
                value = change.get('value', {})

                # Check if this is a message (not a status update)
                messages = value.get('messages', [])

                for message in messages:
                    # Extract the important info
                    phone_number = message.get('from', '')  # sender's phone
                    message_type = message.get('type', '')  # text, image, button, etc.

                    # Get the message text
                    if message_type == 'text':
                        text = message.get('text', {}).get('body', '')
                    elif message_type == 'interactive':
                        # Button or list reply
                        interactive = message.get('interactive', {})
                        interactive_type = interactive.get('type', '')

                        if interactive_type == 'button_reply':
                            text = interactive.get('button_reply', {}).get('id', '') or interactive.get('button_reply', {}).get('title', '')
                        elif interactive_type == 'list_reply':
                            text = interactive.get('list_reply', {}).get('id', '') or interactive.get('list_reply', {}).get('title', '')
                        else:
                            text = ''
                    elif message_type == 'reaction':
                        # Emoji reaction to a message
                        reaction = message.get('reaction', {})
                        emoji = reaction.get('emoji', '')
                        if emoji:
                            text = f'REACTION:{emoji}'
                            message_type = 'reaction'
                        else:
                            text = ''
                    elif message_type == 'image':
                        # Image upload — handle logo uploads
                        image_data = message.get('image', {})
                        media_id = image_data.get('id', '')
                        caption = image_data.get('caption', '')
                        if phone_number and media_id:
                            handle_image_upload(phone_number, media_id, caption)
                        text = ''  # Don't process as text
                    else:
                        # Audio, video, etc. — not handled
                        text = ''

                    if phone_number and text:
                        logger.info(f'Message from {phone_number}: {text}')
                        process_message(phone_number, text, message_type)

        # Always return 200 to WhatsApp (they retry on non-200)
        return response(200, {'status': 'ok'})

    except Exception as e:
        logger.error(f'Error processing message: {str(e)}')
        # Still return 200 — don't make WhatsApp retry on our errors
        return response(200, {'status': 'error', 'message': str(e)})


def process_message(phone_number, text, message_type):
    """
    Process a single message using the main bot router.
    """
    from main import get_bot
    bot = get_bot()
    bot.handle_message(phone_number, text, message_type)


def handle_image_upload(phone_number, media_id, caption=""):
    """
    Handle an image sent by the user — download from Meta, upload to S3, save as logo.
    """
    try:
        import os
        import requests
        import boto3
        from utils.config import get_whatsapp_token

        token = get_whatsapp_token()
        bucket = os.environ.get('GENERATED_FILES_BUCKET', 'kashia-generated-files-dev')

        # Step 1: Get the media URL from Meta
        media_url_resp = requests.get(
            f"https://graph.facebook.com/v17.0/{media_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10
        )
        if media_url_resp.status_code != 200:
            logger.error(f"Failed to get media URL: {media_url_resp.text}")
            _send_text(phone_number, "❌ Could not process the image. Please try again.")
            return

        media_url = media_url_resp.json().get("url", "")
        if not media_url:
            _send_text(phone_number, "❌ Could not process the image. Please try again.")
            return

        # Step 2: Download the image from Meta
        image_resp = requests.get(
            media_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15
        )
        if image_resp.status_code != 200:
            logger.error(f"Failed to download image: {image_resp.status_code}")
            _send_text(phone_number, "❌ Could not download the image. Please try again.")
            return

        # Step 3: Upload to S3
        content_type = image_resp.headers.get("Content-Type", "image/jpeg")
        ext = "png" if "png" in content_type else "jpg"
        s3_key = f"logos/{phone_number}/logo.{ext}"

        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=bucket,
            Key=s3_key,
            Body=image_resp.content,
            ContentType=content_type,
        )

        # Generate a permanent URL (not presigned — logos are reused)
        logo_url = f"https://{bucket}.s3.amazonaws.com/{s3_key}"

        # Step 4: Save to user profile
        from services.database import Database
        db = Database()
        db.update_user_field(phone_number, "logo_url", logo_url)
        db.update_user_field(phone_number, "logo_s3_key", s3_key)

        logger.info(f"Logo saved for {phone_number}: {s3_key}")

        # Notify user
        _send_text(phone_number, (
            "✅ *Logo saved!*\n\n"
            "🖼️ Your business logo has been uploaded.\n"
            "It will appear on all your invoices, receipts, and statements.\n\n"
            "_Send another image anytime to update it._"
        ))

    except Exception as e:
        logger.error(f"Image upload error: {e}")
        _send_text(phone_number, "❌ Something went wrong uploading your logo. Please try again.")


def _send_text(phone_number, text):
    """Quick WhatsApp text send for image handler."""
    try:
        from services.whatsapp_client import WhatsAppClient
        wa = WhatsAppClient()
        wa.send_text(phone_number, text)
    except Exception as e:
        logger.error(f"Send text error in image handler: {e}")

def verify_signature(event):
    """
    Verify that the incoming webhook request is really from Meta.
    Uses HMAC-SHA256 with your App Secret.
    Returns True if valid, False if not.
    """
    from utils.config import get_app_secret

    app_secret = get_app_secret()
    if not app_secret:
        # If no secret configured, skip verification (dev mode)
        return True

    headers = event.get('headers', {}) or {}
    signature = headers.get('x-hub-signature-256') or headers.get('X-Hub-Signature-256', '')

    if not signature or not signature.startswith('sha256='):
        return False

    expected = signature[7:]  # Remove "sha256=" prefix
    body = event.get('body', '') or ''  # Guard against None body

    computed = hmac.new(
        app_secret.encode('utf-8'),
        body.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    # compare_digest(a, b) — put expected first (Meta's value), computed second
    return hmac.compare_digest(expected, computed)


def response(status_code, body):
    """Helper to format Lambda response"""
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json'
        },
        'body': json.dumps(body)
    }

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
    from src.utils.config import get_verify_token

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
                            text = interactive.get('button_reply', {}).get('title', '')
                        elif interactive_type == 'list_reply':
                            text = interactive.get('list_reply', {}).get('title', '')
                        else:
                            text = ''
                    else:
                        # Image, audio, etc. — not handled in MVP
                        text = ''

                    if phone_number and text:
                        logger.info(f'Message from {phone_number}: {text}')
                        # TODO: Route to conversation engine (Step 10)
                        # For now, just echo back
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
    from src.main import get_bot
    bot = get_bot()
    bot.handle_message(phone_number, text, message_type)

def verify_signature(event):
    """
    Verify that the incoming webhook request is really from Meta.
    Uses HMAC-SHA256 with your App Secret.
    Returns True if valid, False if not.
    """
    from src.utils.config import get_app_secret

    app_secret = get_app_secret()
    if not app_secret:
        # If no secret configured, skip verification (dev mode)
        return True

    headers = event.get('headers', {}) or {}
    signature = headers.get('x-hub-signature-256') or headers.get('X-Hub-Signature-256', '')

    if not signature or not signature.startswith('sha256='):
        return False

    expected = signature[7:]  # Remove "sha256=" prefix
    body = event.get('body', '')

    computed = hmac.new(
        app_secret.encode('utf-8'),
        body.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(computed, expected)


def response(status_code, body):
    """Helper to format Lambda response"""
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json'
        },
        'body': json.dumps(body)
    }

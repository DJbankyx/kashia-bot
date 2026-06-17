"""
Step 18: Local Testing for Kashia Bot
Simulates WhatsApp webhook payloads to test all conversation flows.
Uses moto to mock DynamoDB, patches config.get_parameter for SSM.
"""

import os
import sys
import json
import unittest
from unittest.mock import patch, MagicMock
from decimal import Decimal

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load env
from dotenv import load_dotenv
load_dotenv()

import boto3
from moto import mock_aws


# Fake SSM values for local testing
FAKE_SSM_PARAMS = {
    '/kashia/whatsapp-verify-token': 'kashia_verify_2024',
    '/kashia/whatsapp-token': 'test_token_placeholder',
    '/kashia/whatsapp-phone-number-id': '123456789',
    '/kashia/whatsapp-app-secret': 'test_secret_123',
    '/kashia/openai-api-key': 'sk-test-placeholder',
}


def fake_get_parameter(name):
    """Replace SSM calls with local values."""
    if name in FAKE_SSM_PARAMS:
        return FAKE_SSM_PARAMS[name]
    raise Exception(f"Unknown parameter: {name}")


def create_tables(dynamodb):
    """Create all DynamoDB tables for testing."""
    
    dynamodb.create_table(
        TableName=os.environ['USERS_TABLE'],
        KeySchema=[{'AttributeName': 'phone_number', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'phone_number', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST'
    )
    
    dynamodb.create_table(
        TableName=os.environ['TRANSACTIONS_TABLE'],
        KeySchema=[
            {'AttributeName': 'user_id', 'KeyType': 'HASH'},
            {'AttributeName': 'transaction_id', 'KeyType': 'RANGE'}
        ],
        AttributeDefinitions=[
            {'AttributeName': 'user_id', 'AttributeType': 'S'},
            {'AttributeName': 'transaction_id', 'AttributeType': 'S'}
        ],
        BillingMode='PAY_PER_REQUEST'
    )
    
    dynamodb.create_table(
        TableName=os.environ['CONVERSATION_TABLE'],
        KeySchema=[{'AttributeName': 'phone_number', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'phone_number', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST'
    )
    
    dynamodb.create_table(
        TableName=os.environ['ML_FEEDBACK_TABLE'],
        KeySchema=[
            {'AttributeName': 'user_id', 'KeyType': 'HASH'},
            {'AttributeName': 'feedback_id', 'KeyType': 'RANGE'}
        ],
        AttributeDefinitions=[
            {'AttributeName': 'user_id', 'AttributeType': 'S'},
            {'AttributeName': 'feedback_id', 'AttributeType': 'S'}
        ],
        BillingMode='PAY_PER_REQUEST'
    )
    
    dynamodb.create_table(
        TableName=os.environ['MERCHANT_MEMORY_TABLE'],
        KeySchema=[
            {'AttributeName': 'user_id', 'KeyType': 'HASH'},
            {'AttributeName': 'merchant_key', 'KeyType': 'RANGE'}
        ],
        AttributeDefinitions=[
            {'AttributeName': 'user_id', 'AttributeType': 'S'},
            {'AttributeName': 'merchant_key', 'AttributeType': 'S'}
        ],
        BillingMode='PAY_PER_REQUEST'
    )
    
    dynamodb.create_table(
        TableName=os.environ['CONTACTS_TABLE'],
        KeySchema=[
            {'AttributeName': 'user_id', 'KeyType': 'HASH'},
            {'AttributeName': 'contact_id', 'KeyType': 'RANGE'}
        ],
        AttributeDefinitions=[
            {'AttributeName': 'user_id', 'AttributeType': 'S'},
            {'AttributeName': 'contact_id', 'AttributeType': 'S'}
        ],
        BillingMode='PAY_PER_REQUEST'
    )


def make_webhook_event(phone_number, message_text):
    """Create a fake WhatsApp webhook event (API Gateway format)."""
    body = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "BIZ_ACCOUNT_ID",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "2349012345678",
                        "phone_number_id": "123456789"
                    },
                    "contacts": [{
                        "profile": {"name": "Test User"},
                        "wa_id": phone_number
                    }],
                    "messages": [{
                        "from": phone_number,
                        "id": "wamid.TEST123",
                        "timestamp": "1234567890",
                        "text": {"body": message_text},
                        "type": "text"
                    }]
                },
                "field": "messages"
            }]
        }]
    }
    
    return {
        "httpMethod": "POST",
        "headers": {
            "X-Hub-Signature-256": "sha256=test_signature"
        },
        "body": json.dumps(body)
    }


def make_verification_event():
    """Create a webhook verification event (GET request from Meta)."""
    return {
        "httpMethod": "GET",
        "queryStringParameters": {
            "hub.mode": "subscribe",
            "hub.verify_token": "kashia_verify_2024",
            "hub.challenge": "challenge_code_123"
        }
    }


@mock_aws
@patch('src.utils.config.get_parameter', side_effect=fake_get_parameter)
class TestWebhookVerification(unittest.TestCase):
    """Test Meta webhook verification."""
    
    def setUp(self):
        self.dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        create_tables(self.dynamodb)
    
    def test_valid_verification(self, mock_config):
        """Meta sends correct verify_token → we return the challenge."""
        from src.handlers.webhook import lambda_handler
        
        event = make_verification_event()
        response = lambda_handler(event, None)
        
        self.assertEqual(response['statusCode'], 200)
        self.assertEqual(response['body'], 'challenge_code_123')
        print("✅ Webhook verification: PASSED")
    
    def test_invalid_verification(self, mock_config):
        """Wrong verify_token → reject."""
        from src.handlers.webhook import lambda_handler
        
        event = make_verification_event()
        event['queryStringParameters']['hub.verify_token'] = 'wrong_token'
        response = lambda_handler(event, None)
        
        self.assertEqual(response['statusCode'], 403)
        print("✅ Invalid token rejected: PASSED")


@mock_aws
@patch('src.utils.config.get_parameter', side_effect=fake_get_parameter)
class TestNewUserOnboarding(unittest.TestCase):
    """Test first-time user flow."""
    
    def setUp(self):
        self.dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        create_tables(self.dynamodb)
    
    @patch('src.main.get_bot')
    def test_new_user_greeting(self, mock_get_bot, mock_config):
        """First message from new user → bot.handle_message is called."""
        # Create a mock bot
        mock_bot = MagicMock()
        mock_get_bot.return_value = mock_bot
        
        from src.handlers.webhook import lambda_handler
        
        event = make_webhook_event("2348012345678", "Hi")
        response = lambda_handler(event, None)
        
        self.assertEqual(response['statusCode'], 200)
        # Verify the bot received the message
        mock_bot.handle_message.assert_called_once_with(
            "2348012345678", "Hi", "text"
        )
        print("✅ New user greeting: PASSED")
        print(f"   Bot.handle_message called with: {mock_bot.handle_message.call_args}")


@mock_aws
@patch('src.utils.config.get_parameter', side_effect=fake_get_parameter)
class TestTransactionRecording(unittest.TestCase):
    """Test recording a transaction."""
    
    def setUp(self):
        self.dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        create_tables(self.dynamodb)
    
    @patch('src.main.get_bot')
    def test_record_transaction(self, mock_get_bot, mock_config):
        """User sends 'bought rice 5000 from Mama Nkechi' → bot processes it."""
        mock_bot = MagicMock()
        mock_get_bot.return_value = mock_bot
        
        from src.handlers.webhook import lambda_handler
        
        event = make_webhook_event("2348012345678", "bought rice 5000 from Mama Nkechi")
        response = lambda_handler(event, None)
        
        self.assertEqual(response['statusCode'], 200)
        mock_bot.handle_message.assert_called_once_with(
            "2348012345678", "bought rice 5000 from Mama Nkechi", "text"
        )
        print("✅ Transaction recording: PASSED")
        print(f"   Bot received: {mock_bot.handle_message.call_args}")


@mock_aws
@patch('src.utils.config.get_parameter', side_effect=fake_get_parameter)
class TestInteractiveMessages(unittest.TestCase):
    """Test button and list replies."""
    
    def setUp(self):
        self.dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
        create_tables(self.dynamodb)
    
    @patch('src.main.get_bot')
    def test_button_reply(self, mock_get_bot, mock_config):
        """User taps a button → bot receives the button title."""
        mock_bot = MagicMock()
        mock_get_bot.return_value = mock_bot
        
        from src.handlers.webhook import lambda_handler
        
        # Simulate a button reply
        body = {
            "object": "whatsapp_business_account",
            "entry": [{
                "id": "BIZ_ACCOUNT_ID",
                "changes": [{
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "2349012345678",
                            "phone_number_id": "123456789"
                        },
                        "contacts": [{
                            "profile": {"name": "Test User"},
                            "wa_id": "2348012345678"
                        }],
                        "messages": [{
                            "from": "2348012345678",
                            "id": "wamid.TEST456",
                            "timestamp": "1234567890",
                            "type": "interactive",
                            "interactive": {
                                "type": "button_reply",
                                "button_reply": {
                                    "id": "confirm_yes",
                                    "title": "Yes, correct!"
                                }
                            }
                        }]
                    },
                    "field": "messages"
                }]
            }]
        }
        
        event = {
            "httpMethod": "POST",
            "headers": {"X-Hub-Signature-256": "sha256=test"},
            "body": json.dumps(body)
        }
        
        response = lambda_handler(event, None)
        
        self.assertEqual(response['statusCode'], 200)
        mock_bot.handle_message.assert_called_once_with(
            "2348012345678", "Yes, correct!", "interactive"
        )
        print("✅ Button reply: PASSED")


if __name__ == '__main__':
    print("\n" + "="*60)
    print("🧪 KASHIA BOT - LOCAL TESTING")
    print("="*60 + "\n")
    
    unittest.main(verbosity=2)

# src/services/database.py
"""Database Layer - all DynamoDB CRUD operations for Kashia"""

import boto3
import logging
import time
import uuid
from datetime import datetime, timedelta
from boto3.dynamodb.conditions import Key, Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def generate_id():
    """Generate a unique ID (timestamp + random)"""
    timestamp = int(time.time() * 1000)
    random_part = uuid.uuid4().hex[:8]
    return f"{timestamp}_{random_part}"


class Database:
    """Handles all DynamoDB operations for Kashia"""

    def __init__(self, stage="dev"):
        self.dynamodb = boto3.resource('dynamodb')
        self.prefix = f"kashia-"
        self.stage = stage

        # Table references
        self.users = self.dynamodb.Table(f"{self.prefix}users-{stage}")
        self.transactions = self.dynamodb.Table(f"{self.prefix}transactions-{stage}")
        self.sessions = self.dynamodb.Table(f"{self.prefix}conversation-state-{stage}")
        self.feedback = self.dynamodb.Table(f"{self.prefix}ml-feedback-{stage}")
        self.merchants = self.dynamodb.Table(f"{self.prefix}merchant-memory-{stage}")
        self.contacts = self.dynamodb.Table(f"{self.prefix}contacts-{stage}")

    # ==========================================
    # USER OPERATIONS
    # ==========================================

    def create_user(self, phone_number, business_type="trading", business_name=""):
        """Create a new user after onboarding"""
        item = {
            'phone_number': phone_number,
            'business_type': business_type,
            'business_name': business_name,
            'tier': 'free',
            'onboarding_complete': True,
            'transaction_count': 0,
            'created_at': datetime.now().isoformat(),
        }
        self.users.put_item(Item=item)
        logger.info(f"Created user: {phone_number}")
        return item

    def get_user(self, phone_number):
        """Get user by phone number. Returns None if not found."""
        try:
            response = self.users.get_item(Key={'phone_number': phone_number})
            return response.get('Item')
        except Exception as e:
            logger.error(f"Error getting user {phone_number}: {e}")
            return None

    def user_exists(self, phone_number):
        """Check if a user exists"""
        return self.get_user(phone_number) is not None

    def update_user(self, phone_number, updates):
        """
        Update user fields.
        Args:
            updates: dict of fields to update, e.g. {"tier": "basic", "business_name": "My Shop"}
        """
        expressions = []
        values = {}
        names = {}

        for key, value in updates.items():
            safe_key = f"#{key}"
            val_key = f":{key}"
            expressions.append(f"{safe_key} = {val_key}")
            values[val_key] = value
            names[safe_key] = key

        self.users.update_item(
            Key={'phone_number': phone_number},
            UpdateExpression="SET " + ", ".join(expressions),
            ExpressionAttributeValues=values,
            ExpressionAttributeNames=names
        )

    # ==========================================
    # TRANSACTION OPERATIONS
    # ==========================================

    def save_transaction(self, phone_number, amount, tx_type, description,
                         category, sub_category="", vendor="", confidence=0):
        """Save a new transaction"""
        transaction_id = generate_id()
        item = {
            'phone_number': phone_number,
            'transaction_id': transaction_id,
            'amount': int(amount),
            'type': tx_type,  # "income" or "expense"
            'description': description,
            'category': category,
            'sub_category': sub_category,
            'vendor': vendor,
            'confidence': int(confidence),
            'date': datetime.now().strftime('%Y-%m-%d'),
            'created_at': datetime.now().isoformat(),
            'corrected': False,
        }
        self.transactions.put_item(Item=item)

        # Increment user's transaction count
        self.users.update_item(
            Key={'phone_number': phone_number},
            UpdateExpression="SET transaction_count = if_not_exists(transaction_count, :zero) + :one",
            ExpressionAttributeValues={':one': 1, ':zero': 0}
        )

        logger.info(f"Saved transaction: {phone_number} | ₦{amount:,} | {category}")
        return item

    def get_transactions(self, phone_number, limit=20):
        """Get recent transactions for a user (newest first)"""
        try:
            response = self.transactions.query(
                KeyConditionExpression=Key('phone_number').eq(phone_number),
                ScanIndexForward=False,  # newest first
                Limit=limit
            )
            return response.get('Items', [])
        except Exception as e:
            logger.error(f"Error getting transactions: {e}")
            return []

    def get_transactions_by_period(self, phone_number, start_date, end_date):
        """
        Get transactions within a date range.
        Args:
            start_date: "2026-06-01"
            end_date: "2026-06-30"
        """
        try:
            response = self.transactions.query(
                IndexName='date-index',
                KeyConditionExpression=Key('phone_number').eq(phone_number) &
                                       Key('date').between(start_date, end_date)
            )
            return response.get('Items', [])
        except Exception as e:
            logger.error(f"Error querying by period: {e}")
            return []

    def delete_last_transaction(self, phone_number):
        """Delete the most recent transaction (undo)"""
        transactions = self.get_transactions(phone_number, limit=1)
        if transactions:
            tx = transactions[0]
            self.transactions.delete_item(
                Key={
                    'phone_number': phone_number,
                    'transaction_id': tx['transaction_id']
                }
            )
            logger.info(f"Deleted transaction: {tx['transaction_id']}")
            return tx
        return None

    def count_transactions_this_month(self, phone_number):
        """Count how many transactions this month (for tier limits)"""
        now = datetime.now()
        start_date = now.strftime('%Y-%m-01')
        end_date = now.strftime('%Y-%m-%d')
        transactions = self.get_transactions_by_period(phone_number, start_date, end_date)
        return len(transactions)

    # ==========================================
    # CONVERSATION STATE OPERATIONS
    # ==========================================

    def get_session(self, phone_number):
        """Get current conversation state for a user"""
        try:
            response = self.sessions.get_item(Key={'phone_number': phone_number})
            return response.get('Item')
        except Exception as e:
            logger.error(f"Error getting session: {e}")
            return None

    def save_session(self, phone_number, state, context=None):
        """
        Save conversation state.
        Args:
            state: current state (e.g., "IDLE", "RECORDING", "AWAITING_CONFIRMATION")
            context: dict with temporary data (e.g., pending transaction details)
        """
        # TTL: expire after 24 hours of inactivity
        ttl = int(time.time()) + 86400  # 24 hours from now

        item = {
            'phone_number': phone_number,
            'state': state,
            'context': context or {},
            'last_activity': datetime.now().isoformat(),
            'ttl': ttl
        }
        self.sessions.put_item(Item=item)

    def clear_session(self, phone_number):
        """Reset session to IDLE state"""
        self.save_session(phone_number, "IDLE", {})

    # ==========================================
    # ML FEEDBACK OPERATIONS (AI Learning)
    # ==========================================

    def save_feedback(self, phone_number, description, wrong_category, correct_category):
        """Store a correction (user changed AI's suggestion)"""
        feedback_id = generate_id()
        item = {
            'phone_number': phone_number,
            'feedback_id': feedback_id,
            'description': description,
            'wrong_category': wrong_category,
            'correct_category': correct_category,
            'timestamp': datetime.now().isoformat()
        }
        self.feedback.put_item(Item=item)
        logger.info(f"Feedback saved: '{description}' | {wrong_category} → {correct_category}")

    def get_recent_feedback(self, phone_number, limit=5):
        """Get recent corrections for a user (used in AI prompts)"""
        try:
            response = self.feedback.query(
                KeyConditionExpression=Key('phone_number').eq(phone_number),
                ScanIndexForward=False,
                Limit=limit
            )
            return response.get('Items', [])
        except Exception as e:
            logger.error(f"Error getting feedback: {e}")
            return []

    # ==========================================
    # MERCHANT MEMORY OPERATIONS
    # ==========================================

    def save_merchant(self, phone_number, vendor, category, sub_category=""):
        """Remember a vendor's category (skip AI next time)"""
        vendor_normalized = vendor.strip().lower()
        item = {
            'phone_number': phone_number,
            'vendor_normalized': vendor_normalized,
            'vendor_original': vendor,
            'category': category,
            'sub_category': sub_category,
            'usage_count': 1,
            'last_used': datetime.now().isoformat()
        }

        # Use update to increment count if exists
        try:
            self.merchants.update_item(
                Key={
                    'phone_number': phone_number,
                    'vendor_normalized': vendor_normalized
                },
                UpdateExpression="SET category = :cat, sub_category = :sub, "
                                 "usage_count = if_not_exists(usage_count, :zero) + :one, "
                                 "last_used = :now, vendor_original = :orig",
                ExpressionAttributeValues={
                    ':cat': category,
                    ':sub': sub_category,
                    ':one': 1,
                    ':zero': 0,
                    ':now': datetime.now().isoformat(),
                    ':orig': vendor
                }
            )
        except Exception as e:
            # Fallback: just put the item
            self.merchants.put_item(Item=item)

    def get_merchant(self, phone_number, vendor):
        """Check if we remember this vendor's category"""
        vendor_normalized = vendor.strip().lower()
        try:
            response = self.merchants.get_item(
                Key={
                    'phone_number': phone_number,
                    'vendor_normalized': vendor_normalized
                }
            )
            return response.get('Item')
        except Exception as e:
            return None

    # ==========================================
    # CONTACT OPERATIONS (CRM)
    # ==========================================

    def save_contact(self, phone_number, name, contact_type, contact_phone=""):
        """
        Create or update a contact.
        Args:
            contact_type: "customer", "supplier", or "both"
        """
        contact_id = name.strip().lower().replace(" ", "_")
        item = {
            'phone_number': phone_number,
            'contact_id': contact_id,
            'name': name.strip(),
            'type': contact_type,
            'contact_phone': contact_phone,
            'total_paid': 0,
            'total_received': 0,
            'transaction_count': 0,
            'last_transaction_date': datetime.now().strftime('%Y-%m-%d'),
            'created_at': datetime.now().isoformat()
        }
        self.contacts.put_item(Item=item)
        return item

    def get_contacts(self, phone_number, limit=20):
        """Get all contacts for a user"""
        try:
            response = self.contacts.query(
                KeyConditionExpression=Key('phone_number').eq(phone_number),
                Limit=limit
            )
            return response.get('Items', [])
        except Exception as e:
            logger.error(f"Error getting contacts: {e}")
            return []

    def get_contact_by_name(self, phone_number, name):
        """Find a contact by name"""
        contact_id = name.strip().lower().replace(" ", "_")
        try:
            response = self.contacts.get_item(
                Key={
                    'phone_number': phone_number,
                    'contact_id': contact_id
                }
            )
            return response.get('Item')
        except Exception as e:
            return None

    def update_contact_totals(self, phone_number, contact_name, amount, tx_type):
        """Update a contact's total paid/received after a transaction"""
        contact_id = contact_name.strip().lower().replace(" ", "_")
        field = 'total_paid' if tx_type == 'expense' else 'total_received'

        try:
            self.contacts.update_item(
                Key={
                    'phone_number': phone_number,
                    'contact_id': contact_id
                },
                UpdateExpression=f"SET {field} = if_not_exists({field}, :zero) + :amount, "
                                 f"transaction_count = if_not_exists(transaction_count, :zero) + :one, "
                                 f"last_transaction_date = :date",
                ExpressionAttributeValues={
                    ':amount': int(amount),
                    ':one': 1,
                    ':zero': 0,
                    ':date': datetime.now().strftime('%Y-%m-%d')
                }
            )
        except Exception as e:
            logger.error(f"Error updating contact totals: {e}")

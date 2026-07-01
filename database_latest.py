# src/services/database.py
"""Database Layer - all DynamoDB CRUD operations for Kashia"""

import boto3
import re
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
            'custom_categories': [],
            'created_at': datetime.now().isoformat(),
        }
        self.users.put_item(Item=self._sanitize_for_dynamo(item))
        logger.info(f"Created user: {phone_number}")
        return item

    def add_custom_category(self, phone_number, category_name):
        """Add a custom category for a user"""
        self.users.update_item(
            Key={'phone_number': phone_number},
            UpdateExpression="SET custom_categories = list_append(if_not_exists(custom_categories, :empty), :cat)",
            ExpressionAttributeValues={
                ':cat': [category_name],
                ':empty': []
            }
        )
        logger.info(f"Added custom category for {phone_number}: {category_name}")

    def remove_custom_category(self, phone_number, category_name):
        """Remove a custom category for a user"""
        user = self.get_user(phone_number)
        if user:
            categories = user.get('custom_categories', [])
            if category_name in categories:
                categories.remove(category_name)
                self.update_user(phone_number, {'custom_categories': categories})
                return True
        return False

    def get_user_categories(self, phone_number):
        """Get all categories (default + custom) for a user"""
        from services.categorizer import CATEGORIES
        default_cats = list(CATEGORIES.keys())
        user = self.get_user(phone_number)
        custom_cats = user.get('custom_categories', []) if user else []
        return default_cats + custom_cats

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

    @staticmethod
    def _sanitize_for_dynamo(obj):
        """Recursively convert floats to int/Decimal for DynamoDB"""
        from decimal import Decimal
        if isinstance(obj, dict):
            return {k: Database._sanitize_for_dynamo(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [Database._sanitize_for_dynamo(i) for i in obj]
        elif isinstance(obj, float):
            if obj == int(obj):
                return int(obj)
            return Decimal(str(obj))
        return obj

    def save_transaction(self, phone_number, amount, tx_type, description,
                         category, sub_category="", vendor="", confidence=0,
                         item_name=None, brand=None, model=None, size=None,
                         color=None, quantity=None, unit_cost=None,
                         payment_method=None, payment_status=None,
                         extra_details=None, tags=None):
        """Save a new transaction with rich parsed data"""
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

        # Add rich fields (only if they have values — keeps DynamoDB lean)
        if item_name:
            item['item_name'] = item_name
        if brand:
            item['brand'] = brand
        if model:
            item['model'] = model
        if size:
            item['size'] = size
        if color:
            item['color'] = color
        if quantity:
            # Extract numeric part (handles "12 pieces", "1 dozen", etc.)
            if isinstance(quantity, (int, float)):
                item['quantity'] = int(quantity)
            else:
                qty_match = re.match(r'^([\d.]+)', str(quantity))
                if qty_match:
                    item['quantity'] = int(float(qty_match.group(1)))
                else:
                    item['quantity'] = str(quantity)
        if unit_cost:
            if isinstance(unit_cost, (int, float)):
                item['unit_cost'] = int(unit_cost)
            else:
                uc_match = re.match(r'^([\d.]+)', str(unit_cost))
                if uc_match:
                    item['unit_cost'] = int(float(uc_match.group(1)))
        if payment_method:
            item['payment_method'] = payment_method
        if payment_status:
            item['payment_status'] = payment_status
        if extra_details:
            item['extra_details'] = extra_details
        if tags:
            item['tags'] = tags

        self.transactions.put_item(Item=self._sanitize_for_dynamo(item))

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

    def get_recent_transactions(self, phone_number, limit=10):
        """Get recent transactions for editing"""
        return self.get_transactions(phone_number, limit=limit)

    def get_transaction(self, phone_number, transaction_id):
        """Get a specific transaction by ID"""
        try:
            response = self.transactions.get_item(
                Key={
                    'phone_number': phone_number,
                    'transaction_id': transaction_id
                }
            )
            return response.get('Item')
        except Exception as e:
            logger.error(f"Error getting transaction: {e}")
            return None

    def delete_transaction(self, phone_number, transaction_id):
        """Delete a specific transaction by ID"""
        try:
            # Get it first so we can return it
            tx = self.get_transaction(phone_number, transaction_id)
            if tx:
                self.transactions.delete_item(
                    Key={
                        'phone_number': phone_number,
                        'transaction_id': transaction_id
                    }
                )
                logger.info(f"Deleted transaction: {transaction_id}")
                return tx
            return None
        except Exception as e:
            logger.error(f"Error deleting transaction: {e}")
            return None

    def update_transaction(self, phone_number, transaction_id, updates):
        """Update specific fields of a transaction"""
        try:
            update_expr = "SET " + ", ".join([f"#{k} = :{k}" for k in updates.keys()])
            expr_names = {f"#{k}": k for k in updates.keys()}
            expr_values = {f":{k}": v for k, v in updates.items()}

            self.transactions.update_item(
                Key={
                    'phone_number': phone_number,
                    'transaction_id': transaction_id
                },
                UpdateExpression=update_expr,
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values
            )
            logger.info(f"Updated transaction {transaction_id}: {updates}")
            return True
        except Exception as e:
            logger.error(f"Error updating transaction: {e}")
            return False

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
        self.sessions.put_item(Item=self._sanitize_for_dynamo(item))

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
        self.feedback.put_item(Item=self._sanitize_for_dynamo(item))
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
            self.merchants.put_item(Item=self._sanitize_for_dynamo(item))

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
        self.contacts.put_item(Item=self._sanitize_for_dynamo(item))
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
        """Update a contact's total paid/received after a transaction.
        Also saves name and type if contact doesn't exist yet."""
        amount = int(amount)  # Ensure no floats
        contact_id = contact_name.strip().lower().replace(" ", "_")
        contact_type = 'supplier' if tx_type == 'expense' else 'customer'
        field = 'total_paid' if tx_type == 'expense' else 'total_received'
        try:
            self.contacts.update_item(
                Key={
                    'phone_number': phone_number,
                    'contact_id': contact_id
                },
                UpdateExpression=f"SET {field} = if_not_exists({field}, :zero) + :amount, "
                                 f"transaction_count = if_not_exists(transaction_count, :zero) + :one, "
                                 f"last_transaction_date = :date, "
                                 f"#n = if_not_exists(#n, :name), "
                                 f"#t = if_not_exists(#t, :type)",
                ExpressionAttributeNames={
                    '#n': 'name',
                    '#t': 'type'
                },
                ExpressionAttributeValues={
                    ':amount': int(amount),
                    ':one': 1,
                    ':zero': 0,
                    ':date': datetime.now().strftime('%Y-%m-%d'),
                    ':name': contact_name.strip(),
                    ':type': contact_type
                }
            )
        except Exception as e:
            logger.error(f"Error updating contact totals: {e}")

        # Also update analytics
        try:
            self.record_transaction_for_contact(phone_number, contact_name, amount, tx_type)
        except Exception:
            pass  # Non-critical

    # ============================================================
    # DEBT & CREDIT TRACKING
    # ============================================================

    def record_debt(self, phone_number, contact_name, amount, debt_type, description='', due_date=None):
        """
        Record a debt entry.
        debt_type: 'owed_to_me' (customer owes me) or 'i_owe' (I owe supplier)
        """
        contact_id = contact_name.strip().lower().replace(' ', '_')
        field = 'debt_owed_to_me' if debt_type == 'owed_to_me' else 'debt_i_owe'
        try:
            update_expr = (
                f"SET {field} = if_not_exists({field}, :zero) + :amount, "
                f"#n = if_not_exists(#n, :name), "
                f"#t = if_not_exists(#t, :ctype), "
                f"last_transaction_date = :date"
            )
            expr_values = {
                ':amount': int(amount),
                ':zero': 0,
                ':name': contact_name.strip(),
                ':ctype': 'customer' if debt_type == 'owed_to_me' else 'supplier',
                ':date': datetime.now().strftime('%Y-%m-%d'),
            }
            if due_date:
                update_expr += ', due_date = :due'
                expr_values[':due'] = due_date
            if description:
                update_expr += ', last_debt_description = :desc'
                expr_values[':desc'] = description

            self.contacts.update_item(
                Key={'phone_number': phone_number, 'contact_id': contact_id},
                UpdateExpression=update_expr,
                ExpressionAttributeNames={'#n': 'name', '#t': 'type'},
                ExpressionAttributeValues=expr_values
            )
            logger.info(f"Recorded debt: {contact_name} | {debt_type} | ₦{amount:,}")
            return True
        except Exception as e:
            logger.error(f"Error recording debt: {e}")
            return False

    def settle_debt(self, phone_number, contact_name, amount, debt_type):
        """
        Reduce a debt by a payment amount.
        debt_type: 'owed_to_me' or 'i_owe'
        """
        contact_id = contact_name.strip().lower().replace(' ', '_')
        field = 'debt_owed_to_me' if debt_type == 'owed_to_me' else 'debt_i_owe'
        try:
            # Get current debt first
            contact = self.get_contact_by_name(phone_number, contact_name)
            current_debt = int(contact.get(field, 0)) if contact else 0
            new_debt = max(0, current_debt - int(amount))

            self.contacts.update_item(
                Key={'phone_number': phone_number, 'contact_id': contact_id},
                UpdateExpression=f"SET {field} = :new_debt, last_transaction_date = :date",
                ExpressionAttributeValues={
                    ':new_debt': new_debt,
                    ':date': datetime.now().strftime('%Y-%m-%d'),
                }
            )
            logger.info(f"Settled debt: {contact_name} | paid ₦{amount:,} | remaining ₦{new_debt:,}")
            return new_debt
        except Exception as e:
            logger.error(f"Error settling debt: {e}")
            return None

    def get_all_debtors(self, phone_number):
        """Get all contacts who owe the user money"""
        contacts = self.get_contacts(phone_number, limit=100)
        debtors = []
        for c in contacts:
            debt = int(c.get('debt_owed_to_me', 0))
            if debt > 0:
                debtors.append({
                    'name': c.get('name', c.get('contact_id', 'Unknown')),
                    'amount': debt,
                    'last_date': c.get('last_transaction_date', ''),
                    'due_date': c.get('due_date', ''),
                    'description': c.get('last_debt_description', ''),
                    'contact_id': c.get('contact_id', ''),
                })
        debtors.sort(key=lambda x: x['amount'], reverse=True)
        return debtors

    def get_all_creditors(self, phone_number):
        """Get all contacts the user owes money to"""
        contacts = self.get_contacts(phone_number, limit=100)
        creditors = []
        for c in contacts:
            debt = int(c.get('debt_i_owe', 0))
            if debt > 0:
                creditors.append({
                    'name': c.get('name', c.get('contact_id', 'Unknown')),
                    'amount': debt,
                    'last_date': c.get('last_transaction_date', ''),
                    'due_date': c.get('due_date', ''),
                    'description': c.get('last_debt_description', ''),
                    'contact_id': c.get('contact_id', ''),
                })
        creditors.sort(key=lambda x: x['amount'], reverse=True)
        return creditors

    def get_debt_summary(self, phone_number):
        """Get total debt owed to user and total user owes"""
        debtors = self.get_all_debtors(phone_number)
        creditors = self.get_all_creditors(phone_number)
        total_owed_to_me = sum(d['amount'] for d in debtors)
        total_i_owe = sum(c['amount'] for c in creditors)
        return {
            'total_owed_to_me': total_owed_to_me,
            'total_i_owe': total_i_owe,
            'debtors': debtors,
            'creditors': creditors,
            'net': total_owed_to_me - total_i_owe
        }

    def update_contact_note(self, phone_number, contact_name, note):
        """Add or update a note on a contact"""
        contact_id = contact_name.strip().lower().replace(' ', '_')
        try:
            self.contacts.update_item(
                Key={'phone_number': phone_number, 'contact_id': contact_id},
                UpdateExpression="SET notes = :note",
                ExpressionAttributeValues={':note': note}
            )
            return True
        except Exception as e:
            logger.error(f"Error updating contact note: {e}")
            return False

    def get_contact_transactions(self, phone_number, contact_name, limit=10):
        """Get recent transactions involving a specific contact"""
        try:
            all_tx = self.get_transactions(phone_number, limit=100)
            contact_tx = [
                t for t in all_tx
                if contact_name.lower() in t.get('vendor', '').lower()
                or contact_name.lower() in t.get('description', '').lower()
            ]
            return contact_tx[:limit]
        except Exception as e:
            logger.error(f"Error getting contact transactions: {e}")
            return []


    # ============================================================
    # CONTACT CATALOG — Rich Profiles
    # ============================================================

    def update_contact_profile(self, phone_number, contact_name, updates):
        """Update any fields in a contact profile"""
        contact_id = contact_name.strip().lower().replace(' ', '_')
        try:
            set_parts = []
            expr_names = {}
            expr_values = {}
            for i, (k, v) in enumerate(updates.items()):
                safe_key = f"#f{i}"
                val_key = f":v{i}"
                expr_names[safe_key] = k
                expr_values[val_key] = v
                set_parts.append(f"{safe_key} = {val_key}")

            self.contacts.update_item(
                Key={'phone_number': phone_number, 'contact_id': contact_id},
                UpdateExpression="SET " + ", ".join(set_parts),
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values
            )
            return True
        except Exception as e:
            logger.error(f"Error updating contact profile: {e}")
            return False

    def record_transaction_for_contact(self, phone_number, contact_name, amount, tx_type, description=''):
        """
        Update contact analytics after every transaction:
        - purchase/sale frequency
        - average order value
        - first and last purchase dates
        - total lifetime value
        - credit behaviour
        """
        contact_id = contact_name.strip().lower().replace(' ', '_')
        today = datetime.now().strftime('%Y-%m-%d')
        amount = int(amount)

        try:
            # Get current contact data
            contact = self.get_contact_by_name(phone_number, contact_name)
            if not contact:
                return

            # Calculate new analytics
            tx_count = int(contact.get('transaction_count', 0))
            total = int(contact.get('total_received', 0)) if tx_type != 'expense' else int(contact.get('total_paid', 0))
            new_total = total + amount
            new_count = tx_count + 1
            new_avg = new_total // new_count if new_count > 0 else amount

            # First purchase date
            first_date = contact.get('first_purchase_date', today)

            # Days since last transaction
            last_date = contact.get('last_transaction_date', today)
            try:
                last_dt = datetime.strptime(last_date, '%Y-%m-%d')
                days_between = (datetime.now() - last_dt).days
            except Exception:
                days_between = 0

            # Update purchase frequency (rolling average days between purchases)
            old_freq = int(contact.get('avg_days_between_purchases', 0))
            if old_freq > 0 and days_between > 0:
                new_freq = (old_freq + days_between) // 2
            elif days_between > 0:
                new_freq = days_between
            else:
                new_freq = old_freq

            field = 'total_received' if tx_type != 'expense' else 'total_paid'

            self.contacts.update_item(
                Key={'phone_number': phone_number, 'contact_id': contact_id},
                UpdateExpression=(
                    f"SET {field} = if_not_exists({field}, :zero) + :amount, "
                    f"transaction_count = if_not_exists(transaction_count, :zero) + :one, "
                    f"last_transaction_date = :today, "
                    f"first_purchase_date = if_not_exists(first_purchase_date, :today), "
                    f"avg_order_value = :avg, "
                    f"avg_days_between_purchases = :freq, "
                    f"lifetime_value = if_not_exists(lifetime_value, :zero) + :amount"
                ),
                ExpressionAttributeValues={
                    ':amount': amount,
                    ':one': 1,
                    ':zero': 0,
                    ':today': today,
                    ':avg': new_avg,
                    ':freq': new_freq,
                }
            )
        except Exception as e:
            logger.error(f"Error recording contact transaction analytics: {e}")

    def get_contact_analytics(self, phone_number, contact_name):
        """Get full analytics for a contact"""
        contact = self.get_contact_by_name(phone_number, contact_name)
        if not contact:
            return None

        today = datetime.now()
        last_date = contact.get('last_transaction_date', '')
        first_date = contact.get('first_purchase_date', '')

        # Days since last purchase
        days_inactive = None
        if last_date:
            try:
                last_dt = datetime.strptime(last_date, '%Y-%m-%d')
                days_inactive = (today - last_dt).days
            except Exception:
                pass

        # Relationship duration
        relationship_days = None
        if first_date:
            try:
                first_dt = datetime.strptime(first_date, '%Y-%m-%d')
                relationship_days = (today - first_dt).days
            except Exception:
                pass

        # Credit behaviour
        debt_owed = int(contact.get('debt_owed_to_me', 0))
        total_received = int(contact.get('total_received', 0))
        credit_ratio = round(debt_owed / total_received * 100) if total_received > 0 else 0

        return {
            'name': contact.get('name', contact_name),
            'type': contact.get('type', 'contact'),
            'contact_phone': contact.get('contact_phone', ''),
            'total_received': int(contact.get('total_received', 0)),
            'total_paid': int(contact.get('total_paid', 0)),
            'lifetime_value': int(contact.get('lifetime_value', 0)),
            'transaction_count': int(contact.get('transaction_count', 0)),
            'avg_order_value': int(contact.get('avg_order_value', 0)),
            'avg_days_between': int(contact.get('avg_days_between_purchases', 0)),
            'first_purchase_date': first_date,
            'last_transaction_date': last_date,
            'days_inactive': days_inactive,
            'relationship_days': relationship_days,
            'debt_owed_to_me': debt_owed,
            'debt_i_owe': int(contact.get('debt_i_owe', 0)),
            'credit_ratio': credit_ratio,
            'credit_limit': int(contact.get('credit_limit', 0)),
            'credit_days': int(contact.get('credit_days', 0)),
            'notes': contact.get('notes', ''),
            'payment_history': contact.get('payment_history', []),
            'preferred_products': contact.get('preferred_products', []),
            'last_payment_date': contact.get('last_payment_date', ''),
            'last_payment_amount': int(contact.get('last_payment_amount', 0)),
        }

    def set_credit_terms(self, phone_number, contact_name, credit_limit=0, credit_days=0):
        """Set credit limit and credit days for a contact"""
        contact_id = contact_name.strip().lower().replace(' ', '_')
        try:
            self.contacts.update_item(
                Key={'phone_number': phone_number, 'contact_id': contact_id},
                UpdateExpression="SET credit_limit = :limit, credit_days = :days, #n = if_not_exists(#n, :name)",
                ExpressionAttributeNames={'#n': 'name'},
                ExpressionAttributeValues={
                    ':limit': int(credit_limit),
                    ':days': int(credit_days),
                    ':name': contact_name.strip()
                }
            )
            return True
        except Exception as e:
            logger.error(f"Error setting credit terms: {e}")
            return False

    def get_top_contacts(self, phone_number, contact_type='customer', limit=5):
        """Get top contacts by total value"""
        contacts = self.get_contacts(phone_number, limit=100)
        filtered = [c for c in contacts if c.get('type', '') in [contact_type, 'both']]
        if contact_type == 'customer':
            filtered.sort(key=lambda x: int(x.get('total_received', 0)), reverse=True)
        else:
            filtered.sort(key=lambda x: int(x.get('total_paid', 0)), reverse=True)
        return filtered[:limit]

    def get_inactive_contacts(self, phone_number, days=30):
        """Get contacts who haven't transacted in X days"""
        contacts = self.get_contacts(phone_number, limit=100)
        inactive = []
        cutoff = (datetime.now() - __import__('datetime').timedelta(days=days)).strftime('%Y-%m-%d')
        for c in contacts:
            last = c.get('last_transaction_date', '')
            if last and last < cutoff:
                inactive.append(c)
        inactive.sort(key=lambda x: x.get('last_transaction_date', ''))
        return inactive

    # ============================================================
    # PRODUCT REGISTRY — FULL HIERARCHY
    # Product → Subcategory/Brand → Series/Model → Attributes
    # ============================================================

    def get_product_catalog(self, phone_number):
        """Get user's full product catalog"""
        user = self.get_user(phone_number)
        if not user:
            return {}
        return user.get('product_catalog', {})

    def save_product_catalog(self, phone_number, catalog):
        """Save entire product catalog"""
        self.users.update_item(
            Key={'phone_number': phone_number},
            UpdateExpression='SET product_catalog = :catalog',
            ExpressionAttributeValues={':catalog': catalog}
        )

    def get_product_list(self, phone_number):
        """Get list of top-level product names"""
        catalog = self.get_product_catalog(phone_number)
        return list(catalog.get('products', {}).keys())

    def add_product(self, phone_number, product_name):
        """Add a top-level product (Shoes, Bags, Clothes)"""
        catalog = self.get_product_catalog(phone_number)
        products = catalog.setdefault('products', {})
        key = product_name.strip().title()
        if key not in products:
            products[key] = {
                'subcategories': {},
                'attributes': {},
                'conversions': {}
            }
            catalog['products'] = products
            self.save_product_catalog(phone_number, catalog)
            return True
        return False

    def remove_product(self, phone_number, product_name):
        """Remove a top-level product"""
        catalog = self.get_product_catalog(phone_number)
        products = catalog.get('products', {})
        key = product_name.strip().title()
        if key in products:
            del products[key]
            catalog['products'] = products
            self.save_product_catalog(phone_number, catalog)
            return True
        return False

    def add_subcategory(self, phone_number, product_name, subcategory_name):
        """Add a subcategory/brand under a product (Nike under Shoes)"""
        catalog = self.get_product_catalog(phone_number)
        products = catalog.get('products', {})
        p_key = product_name.strip().title()
        if p_key not in products:
            products[p_key] = {'subcategories': {}, 'attributes': {}, 'conversions': {}}
        sub_key = subcategory_name.strip().title()
        if sub_key not in products[p_key]['subcategories']:
            products[p_key]['subcategories'][sub_key] = {
                'series': {},
                'attributes': {},
                'conversions': {}
            }
            catalog['products'] = products
            self.save_product_catalog(phone_number, catalog)
            return True
        return False

    def remove_subcategory(self, phone_number, product_name, subcategory_name):
        """Remove a subcategory from a product"""
        catalog = self.get_product_catalog(phone_number)
        products = catalog.get('products', {})
        p_key = product_name.strip().title()
        sub_key = subcategory_name.strip().title()
        if p_key in products and sub_key in products[p_key].get('subcategories', {}):
            del products[p_key]['subcategories'][sub_key]
            catalog['products'] = products
            self.save_product_catalog(phone_number, catalog)
            return True
        return False

    def add_series(self, phone_number, product_name, subcategory_name, series_name):
        """Add a series/model under a subcategory (Air Force 1 under Nike)"""
        catalog = self.get_product_catalog(phone_number)
        products = catalog.get('products', {})
        p_key = product_name.strip().title()
        sub_key = subcategory_name.strip().title()
        series_key = series_name.strip().title()

        if p_key not in products:
            products[p_key] = {'subcategories': {}, 'attributes': {}, 'conversions': {}}
        if sub_key not in products[p_key]['subcategories']:
            products[p_key]['subcategories'][sub_key] = {'series': {}, 'attributes': {}, 'conversions': {}}
        if series_key not in products[p_key]['subcategories'][sub_key]['series']:
            products[p_key]['subcategories'][sub_key]['series'][series_key] = {'attributes': {}}
            catalog['products'] = products
            self.save_product_catalog(phone_number, catalog)
            return True
        return False

    def set_attributes(self, phone_number, product_name, attribute, values, subcategory=None, series=None):
        """Set attribute values at any level of the hierarchy.
        
        Examples:
            set_attributes(phone, 'Shoes', 'size', ['38','39','40'])  — product level
            set_attributes(phone, 'Shoes', 'size', ['38','39'], subcategory='Nike')  — subcategory level
            set_attributes(phone, 'Shoes', 'size', ['38','39'], subcategory='Nike', series='Air Force 1')  — series level
        """
        catalog = self.get_product_catalog(phone_number)
        products = catalog.get('products', {})
        p_key = product_name.strip().title()

        if p_key not in products:
            products[p_key] = {'subcategories': {}, 'attributes': {}, 'conversions': {}}

        if series and subcategory:
            sub_key = subcategory.strip().title()
            series_key = series.strip().title()
            if sub_key not in products[p_key]['subcategories']:
                products[p_key]['subcategories'][sub_key] = {'series': {}, 'attributes': {}, 'conversions': {}}
            if series_key not in products[p_key]['subcategories'][sub_key]['series']:
                products[p_key]['subcategories'][sub_key]['series'][series_key] = {'attributes': {}}
            products[p_key]['subcategories'][sub_key]['series'][series_key]['attributes'][attribute.lower()] = values
        elif subcategory:
            sub_key = subcategory.strip().title()
            if sub_key not in products[p_key]['subcategories']:
                products[p_key]['subcategories'][sub_key] = {'series': {}, 'attributes': {}, 'conversions': {}}
            products[p_key]['subcategories'][sub_key]['attributes'][attribute.lower()] = values
        else:
            products[p_key]['attributes'][attribute.lower()] = values

        catalog['products'] = products
        self.save_product_catalog(phone_number, catalog)

    def set_conversions(self, phone_number, product_name, conversions, subcategory=None):
        """Set unit conversions at product or subcategory level.
        conversions = {'1 carton': '10 pairs', '1 dozen': '12 pairs'}
        """
        catalog = self.get_product_catalog(phone_number)
        products = catalog.get('products', {})
        p_key = product_name.strip().title()

        if p_key not in products:
            products[p_key] = {'subcategories': {}, 'attributes': {}, 'conversions': {}}

        if subcategory:
            sub_key = subcategory.strip().title()
            if sub_key not in products[p_key]['subcategories']:
                products[p_key]['subcategories'][sub_key] = {'series': {}, 'attributes': {}, 'conversions': {}}
            products[p_key]['subcategories'][sub_key]['conversions'].update(conversions)
        else:
            products[p_key]['conversions'].update(conversions)

        catalog['products'] = products
        self.save_product_catalog(phone_number, catalog)

    def get_product_details(self, phone_number, product_name):
        """Get full details for one product"""
        catalog = self.get_product_catalog(phone_number)
        return catalog.get('products', {}).get(product_name.strip().title(), None)

    def get_catalog_for_ai(self, phone_number):
        """Build a compact text summary of catalog for AI prompt injection"""
        catalog = self.get_product_catalog(phone_number)
        products = catalog.get('products', {})
        if not products:
            return ""

        lines = []
        for p_name, p_data in products.items():
            subcats = p_data.get('subcategories', {})
            attrs = p_data.get('attributes', {})
            convs = p_data.get('conversions', {})

            if subcats:
                sub_parts = []
                for sub_name, sub_data in subcats.items():
                    series = sub_data.get('series', {})
                    sub_attrs = sub_data.get('attributes', {})
                    sub_convs = sub_data.get('conversions', {})

                    if series:
                        series_names = list(series.keys())
                        series_str = f"{sub_name} (models: {', '.join(series_names)})"
                    else:
                        series_str = sub_name

                    # Add subcategory attributes
                    if sub_attrs:
                        attr_parts = []
                        for attr, vals in sub_attrs.items():
                            if vals:
                                attr_parts.append(f"{attr}: {', '.join(vals[:10])}")
                            else:
                                attr_parts.append(attr)
                        series_str += f" [{'; '.join(attr_parts)}]"

                    sub_parts.append(series_str)

                line = f"PRODUCT: {p_name} -> {', '.join(sub_parts)}"
            else:
                line = f"PRODUCT: {p_name}"

            # Product-level attributes
            if attrs:
                attr_strs = []
                for attr, vals in attrs.items():
                    if vals:
                        attr_strs.append(f"{attr}: {', '.join(vals[:10])}")
                    else:
                        attr_strs.append(attr)
                line += f" | Attributes: {'; '.join(attr_strs)}"

            # Conversions
            if convs:
                conv_strs = [f"{k}={v}" for k, v in convs.items()]
                line += f" | Units: {', '.join(conv_strs)}"

            lines.append(line)

        return "\n".join(lines)


    def get_primary_unit(self, phone_number, product_name):
        """Get the primary/base unit for a product"""
        catalog = self.get_product_catalog(phone_number)
        products = catalog.get('products', {})
        if product_name in products:
            return products[product_name].get('primary_unit', 'pieces')
        return 'pieces'

    def set_primary_unit(self, phone_number, product_name, unit):
        """Set the primary/base unit for a product"""
        catalog = self.get_product_catalog(phone_number)
        products = catalog.get('products', {})
        if product_name in products:
            products[product_name]['primary_unit'] = unit.lower().strip()
            self.save_product_catalog(phone_number, catalog)
            return True
        return False

    def get_conversions_for_product(self, phone_number, product_name, subcategory=None):
        """Get all conversions for a product (subcategory overrides product level)"""
        catalog = self.get_product_catalog(phone_number)
        products = catalog.get('products', {})
        if product_name not in products:
            return {}
        p_data = products[product_name]
        conversions = dict(p_data.get('conversions', {}))
        if subcategory:
            subcats = p_data.get('subcategories', {})
            if subcategory in subcats:
                conversions.update(subcats[subcategory].get('conversions', {}))
        return conversions

    def convert_to_base(self, phone_number, product_name, quantity_raw, unit_raw, subcategory=None):
        """Convert quantity+unit to base units using registered conversions."""
        import re as _re
        conversions = self.get_conversions_for_product(phone_number, product_name, subcategory)
        primary_unit = self.get_primary_unit(phone_number, product_name)

        if not conversions or not unit_raw:
            return (quantity_raw, unit_raw or primary_unit, None)

        unit_lower = str(unit_raw).lower().strip()

        for conv_key, conv_value in conversions.items():
            # Normalize: "1dozen" -> "1 dozen", "12pieces" -> "12 pieces"
            norm_key = _re.sub(r'(\d)(\D)', r'\1 \2', conv_key.strip())
            norm_val = _re.sub(r'(\d)(\D)', r'\1 \2', conv_value.strip())
            key_match = _re.match(r'^(\d+)\s+(.+)$', norm_key)
            val_match = _re.match(r'^(\d+)\s+(.+)$', norm_val)

            if key_match and val_match:
                key_num = int(key_match.group(1))
                key_unit = key_match.group(2).lower().strip()
                val_num = int(val_match.group(1))
                val_unit = val_match.group(2).lower().strip()

                if (unit_lower == key_unit or
                    unit_lower == key_unit + 's' or
                    unit_lower + 's' == key_unit or
                    unit_lower.rstrip('s') == key_unit.rstrip('s')):

                    base_qty = int(quantity_raw) * (val_num // key_num)
                    conv_str = f"{conv_key} = {conv_value}"
                    return (base_qty, val_unit, conv_str)

        return (quantity_raw, unit_raw or primary_unit, None)

    def convert_from_base(self, phone_number, product_name, base_quantity, target_unit, subcategory=None):
        """Convert FROM base units TO a target unit. Returns (qty, unit) or None."""
        import re as _re
        conversions = self.get_conversions_for_product(phone_number, product_name, subcategory)
        target_lower = target_unit.lower().strip()

        for conv_key, conv_value in conversions.items():
            key_match = _re.match(r'^(\d+)\s+(.+)$', conv_key.strip())
            val_match = _re.match(r'^(\d+)\s+(.+)$', conv_value.strip())

            if key_match and val_match:
                key_num = int(key_match.group(1))
                key_unit = key_match.group(2).lower().strip()
                val_num = int(val_match.group(1))

                if (target_lower == key_unit or
                    target_lower == key_unit + 's' or
                    target_lower.rstrip('s') == key_unit.rstrip('s')):
                    if val_num > 0:
                        converted = base_quantity * key_num / val_num
                        if converted == int(converted):
                            return (int(converted), target_unit)
                        return (round(converted, 1), target_unit)
        return None

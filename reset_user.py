"""
reset_user.py — Full data reset for a single user across all DynamoDB tables.
Usage: python3 reset_user.py
"""

import boto3
from boto3.dynamodb.conditions import Key

PHONE  = '2347060475064'
STAGE  = 'dev'
REGION = 'eu-west-1'

dynamodb = boto3.resource('dynamodb', region_name=REGION)

def delete_all_rows(table, pk_name, sk_name, phone):
    """Query all rows for phone_number and batch delete them."""
    resp  = table.query(KeyConditionExpression=Key(pk_name).eq(phone))
    items = resp.get('Items', [])
    while 'LastEvaluatedKey' in resp:
        resp = table.query(
            KeyConditionExpression=Key(pk_name).eq(phone),
            ExclusiveStartKey=resp['LastEvaluatedKey']
        )
        items.extend(resp.get('Items', []))

    if not items:
        return 0

    with table.batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={
                pk_name: phone,
                sk_name: item[sk_name]
            })
    return len(items)


print(f"\n🗑️  Full reset for: {PHONE}\n{'─'*40}")

# 1. User record (single item, no sort key)
users = dynamodb.Table(f'kashia-users-{STAGE}')
resp = users.get_item(Key={'phone_number': PHONE})
if resp.get('Item'):
    users.delete_item(Key={'phone_number': PHONE})
    print(f'✅ User profile deleted')
else:
    print(f'ℹ️  No user profile found')

# 2. Session (single item, no sort key)
sessions = dynamodb.Table(f'kashia-conversation-state-{STAGE}')
resp = sessions.get_item(Key={'phone_number': PHONE})
if resp.get('Item'):
    sessions.delete_item(Key={'phone_number': PHONE})
    print(f'✅ Session deleted')
else:
    print(f'ℹ️  No session found')

# 3. Transactions
n = delete_all_rows(
    dynamodb.Table(f'kashia-transactions-{STAGE}'),
    'phone_number', 'transaction_id', PHONE
)
print(f'✅ Transactions deleted: {n}')

# 4. Contacts
n = delete_all_rows(
    dynamodb.Table(f'kashia-contacts-{STAGE}'),
    'phone_number', 'contact_id', PHONE
)
print(f'✅ Contacts deleted: {n}')

# 5. ML Feedback
n = delete_all_rows(
    dynamodb.Table(f'kashia-ml-feedback-{STAGE}'),
    'phone_number', 'feedback_id', PHONE
)
print(f'✅ ML Feedback deleted: {n}')

# 6. Merchant Memory
n = delete_all_rows(
    dynamodb.Table(f'kashia-merchant-memory-{STAGE}'),
    'phone_number', 'vendor_normalized', PHONE
)
print(f'✅ Merchant memory deleted: {n}')

print(f'\n🎉 Done! {PHONE} will go through onboarding as a new user.')

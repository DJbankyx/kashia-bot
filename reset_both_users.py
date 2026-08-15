"""
reset_both_users.py — Full data wipe for both test users across ALL 6 tables.
Usage: python3 reset_both_users.py
"""

import boto3
from boto3.dynamodb.conditions import Key

PHONES = ['2347060475064', '2348137431182']
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


for phone in PHONES:
    print(f"\n🗑️  Full reset for: {phone}\n{'─'*40}")

    # 1. User record (single item, no sort key)
    users = dynamodb.Table(f'kashia-users-{STAGE}')
    resp = users.get_item(Key={'phone_number': phone})
    if resp.get('Item'):
        users.delete_item(Key={'phone_number': phone})
        print(f'  ✅ User profile deleted')
    else:
        print(f'  ℹ️  No user profile found')

    # 2. Session (single item, no sort key)
    sessions = dynamodb.Table(f'kashia-conversation-state-{STAGE}')
    resp = sessions.get_item(Key={'phone_number': phone})
    if resp.get('Item'):
        sessions.delete_item(Key={'phone_number': phone})
        print(f'  ✅ Session deleted')
    else:
        print(f'  ℹ️  No session found')

    # 3. Transactions
    n = delete_all_rows(
        dynamodb.Table(f'kashia-transactions-{STAGE}'),
        'phone_number', 'transaction_id', phone
    )
    print(f'  ✅ Transactions deleted: {n}')

    # 4. Contacts
    n = delete_all_rows(
        dynamodb.Table(f'kashia-contacts-{STAGE}'),
        'phone_number', 'contact_id', phone
    )
    print(f'  ✅ Contacts deleted: {n}')

    # 5. ML Feedback
    n = delete_all_rows(
        dynamodb.Table(f'kashia-ml-feedback-{STAGE}'),
        'phone_number', 'feedback_id', phone
    )
    print(f'  ✅ ML Feedback deleted: {n}')

    # 6. Merchant Memory
    n = delete_all_rows(
        dynamodb.Table(f'kashia-merchant-memory-{STAGE}'),
        'phone_number', 'vendor_normalized', phone
    )
    print(f'  ✅ Merchant memory deleted: {n}')

print(f'\n{'─'*40}')
print(f'🎉 Both users fully wiped. Clean slate!')
print(f'   Both will go through onboarding as new users.')

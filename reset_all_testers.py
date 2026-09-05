#!/usr/bin/env python3
"""Reset both test users"""
import boto3
from boto3.dynamodb.conditions import Key

USERS = ['2347060475064', '2348137431182']
STAGE = 'dev'
REGION = 'eu-west-1'

dynamodb = boto3.resource('dynamodb', region_name=REGION)

tables_config = [
    ('kashia-users-dev', 'phone_number', None),
    ('kashia-conversation-state-dev', 'phone_number', None),
    ('kashia-transactions-dev', 'phone_number', 'transaction_id'),
    ('kashia-contacts-dev', 'phone_number', 'contact_id'),
    ('kashia-ml-feedback-dev', 'phone_number', 'feedback_id'),
    ('kashia-merchant-memory-dev', 'phone_number', 'vendor_normalized'),
]

for phone in USERS:
    print(f"\n🗑️  Resetting: {phone}")
    print("─" * 40)
    for table_name, pk, sk in tables_config:
        table = dynamodb.Table(table_name)
        try:
            if sk is None:
                resp = table.get_item(Key={pk: phone})
                if resp.get('Item'):
                    table.delete_item(Key={pk: phone})
                    print(f"  ✅ {table_name}")
                else:
                    print(f"  ⬜ {table_name} (empty)")
            else:
                resp = table.query(KeyConditionExpression=Key(pk).eq(phone))
                items = resp.get('Items', [])
                if items:
                    with table.batch_writer() as batch:
                        for item in items:
                            batch.delete_item(Key={pk: phone, sk: item[sk]})
                    print(f"  ✅ {table_name} ({len(items)} deleted)")
                else:
                    print(f"  ⬜ {table_name} (empty)")
        except Exception as e:
            print(f"  ❌ {table_name}: {e}")

print("\n🎉 Done! Both users will start fresh onboarding.")

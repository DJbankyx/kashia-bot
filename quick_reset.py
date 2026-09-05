#!/usr/bin/env python3
"""Quick reset for test user 2347060475064"""
import sys
sys.path.insert(0, '/home/bankyunix/projects/kashia-bot/venv/lib/python3.12/site-packages')

import boto3
from boto3.dynamodb.conditions import Key

PHONE = '2347060475064'
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

for table_name, pk, sk in tables_config:
    table = dynamodb.Table(table_name)
    try:
        if sk is None:
            resp = table.get_item(Key={pk: PHONE})
            if resp.get('Item'):
                table.delete_item(Key={pk: PHONE})
                print(f"DELETED from {table_name}")
            else:
                print(f"EMPTY {table_name}")
        else:
            resp = table.query(KeyConditionExpression=Key(pk).eq(PHONE))
            items = resp.get('Items', [])
            if items:
                with table.batch_writer() as batch:
                    for item in items:
                        batch.delete_item(Key={pk: PHONE, sk: item[sk]})
                print(f"DELETED {len(items)} from {table_name}")
            else:
                print(f"EMPTY {table_name}")
    except Exception as e:
        print(f"ERROR {table_name}: {e}")

print("DONE")

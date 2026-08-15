"""
cleanup_contacts.py — Remove bad contact records (Sold, Bought, No Price?, etc.)
Usage: python3 cleanup_contacts.py
"""

import boto3
from boto3.dynamodb.conditions import Key

PHONE  = '2347060475064'
STAGE  = 'dev'
REGION = 'eu-west-1'

# Contact names to remove (case-insensitive match)
BAD_NAMES = {
    'sold', 'bought', 'paid', 'received',
    'no price?', 'no price', 'unknown',
    'sale', 'purchase', 'expense',
}

dynamodb = boto3.resource('dynamodb', region_name=REGION)
contacts = dynamodb.Table(f'kashia-contacts-{STAGE}')

print(f"\n🧹 Cleaning bad contacts for: {PHONE}\n{'─'*40}")

# Query all contacts
resp = contacts.query(KeyConditionExpression=Key('phone_number').eq(PHONE))
items = resp.get('Items', [])
while 'LastEvaluatedKey' in resp:
    resp = contacts.query(
        KeyConditionExpression=Key('phone_number').eq(PHONE),
        ExclusiveStartKey=resp['LastEvaluatedKey']
    )
    items.extend(resp.get('Items', []))

print(f"Found {len(items)} total contacts.\n")

deleted = 0
for item in items:
    name = item.get('name', '').strip()
    contact_id = item.get('contact_id', '')

    if name.lower() in BAD_NAMES:
        contacts.delete_item(Key={
            'phone_number': PHONE,
            'contact_id': contact_id,
        })
        print(f"  🗑️  Deleted: '{name}' (id: {contact_id})")
        deleted += 1

print(f"\n{'─'*40}")
print(f"✅ Removed {deleted} bad contacts. {len(items) - deleted} remaining.")

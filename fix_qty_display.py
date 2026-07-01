engine_path = '/home/bankyunix/projects/kashia-bot/src/services/conversation_engine.py'

with open(engine_path, 'r') as f:
    content = f.read()

# Find and replace the Qty display line to include base conversion
old = """        if result.get('quantity'):
            details.append(f"Qty: {result['quantity']}")"""

new = """        if result.get('quantity'):
            qty_display = f"Qty: {result['quantity']}"
            if pending.get('base_quantity'):
                qty_display += f" (= {pending['base_quantity']} {pending['base_unit']})"
            details.append(qty_display)"""

if old in content:
    content = content.replace(old, new, 1)
    print("✅ Fixed: Qty display now shows base conversion")
else:
    print("❌ Could not find Qty pattern")

with open(engine_path, 'w') as f:
    f.write(content)

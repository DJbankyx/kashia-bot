import re

db_path = '/home/bankyunix/projects/kashia-bot/src/services/database.py'

with open(db_path, 'r') as f:
    content = f.read()

# In convert_to_base, add normalization before regex matching
# Insert space between number and letter: "1dozen" -> "1 dozen"

old = """        for conv_key, conv_value in conversions.items():
            key_match = _re.match(r'^(\\d+)\\s+(.+)$', conv_key.strip())
            val_match = _re.match(r'^(\\d+)\\s+(.+)$', conv_value.strip())"""

new = """        for conv_key, conv_value in conversions.items():
            # Normalize: "1dozen" -> "1 dozen", "12pieces" -> "12 pieces"
            norm_key = _re.sub(r'(\\d)(\\D)', r'\\1 \\2', conv_key.strip())
            norm_val = _re.sub(r'(\\d)(\\D)', r'\\1 \\2', conv_value.strip())
            key_match = _re.match(r'^(\\d+)\\s+(.+)$', norm_key)
            val_match = _re.match(r'^(\\d+)\\s+(.+)$', norm_val)"""

if old in content:
    content = content.replace(old, new, 1)
    print("✅ Fixed: convert_to_base now normalizes '1dozen' -> '1 dozen'")
else:
    print("❌ Pattern not found")

with open(db_path, 'w') as f:
    f.write(content)

"""Quick syntax check for all feature modules."""
import sys
sys.path.insert(0, "src")

try:
    import features.catalog
    print("✅ catalog.py OK")
except Exception as e:
    print(f"❌ catalog.py: {e}")

try:
    import features.transactions
    print("✅ transactions.py OK")
except Exception as e:
    print(f"❌ transactions.py: {e}")

try:
    import core.router
    print("✅ router.py OK")
except Exception as e:
    print(f"❌ router.py: {e}")

try:
    import main
    print("✅ main.py OK")
except Exception as e:
    print(f"❌ main.py: {e}")

print("\nDone. If all show ✅, deploy should work.")

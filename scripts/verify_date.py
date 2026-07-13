
import email.utils
from datetime import datetime

dates = [
    "Wed, 22 Apr 2026 11:33:51 GMT",
    "2026-04-22T11:33:51Z",
    "Invalid Date"
]

for d in dates:
    print(f"Original: {d}")
    try:
        # RSS Fix
        dt = email.utils.parsedate_to_datetime(d)
        print(f"  Parsed (RSS): {dt.strftime('%Y-%m-%d %H:%M')}")
    except Exception:
        pass
        
    try:
        # GNews Fix
        if "T" in d:
             print(f"  Standardized (GNews): {d.replace('T', ' ').replace('Z', '')[:16]}")
    except Exception:
        pass

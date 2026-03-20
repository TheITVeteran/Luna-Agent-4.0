import json, sys
import re
params = json.load(sys.stdin)
result = {re.sub(r'\w+', lambda x: x.group(0)[:10], key): value for key, value in params.items()}
print(result)
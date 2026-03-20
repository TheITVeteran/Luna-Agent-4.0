import re, json, sys
params = json.load(sys.stdin)
result = {re.sub(r'^\w', lambda x: x.group().upper(), k): v for k, v in params.items()}
print(result)
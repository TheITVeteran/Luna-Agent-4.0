import json, sys
params = json.load(sys.stdin)
result = [x.lower() for x in params.values() if not next((y for y in str(y) if y.isupper()), False)]
print(result)
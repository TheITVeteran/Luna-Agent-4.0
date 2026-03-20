import json, sys
params = json.load(sys.stdin)

result = {key: value.strip() for key, value in params.items()}
print(result)
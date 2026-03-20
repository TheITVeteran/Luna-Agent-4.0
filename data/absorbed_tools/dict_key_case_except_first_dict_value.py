import json, sys

params = json.load(sys.stdin)
result = {key.case() if isinstance(value, dict) else value: value 
          for key, value in params.items()}
print(result)
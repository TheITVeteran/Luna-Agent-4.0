import json, sys
params = json.load(sys.stdin)

def change_keys_case(data, lower=True):
    if isinstance(data, dict):
        return {key.lower() if lower else key.upper(): value for key, value in data.items()}
    elif isinstance(data, list):
        return [change_keys_case(item, lower) if not isinstance(item, str) else item for item in data]
    return data

result = change_keys_case(params)
print(result)
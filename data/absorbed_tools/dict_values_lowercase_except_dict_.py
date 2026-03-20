import json, sys
params = json.load(sys.stdin)

def process_dict_values(params):
    result = {}
    for key, value in params.items():
        if isinstance(value, dict):  # if value is a dictionary
            result[key] = process_dict_values(value)
        else:  # convert value to lowercase
            result[key] = str(value).lower()
    return result

result = process_dict_values(params)
print(json.dumps(result, indent=4))
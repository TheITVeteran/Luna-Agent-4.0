import json, sys
import random

params = json.load(sys.stdin)

def dict_case_random(params):
    result = {k:v for k,v in params.items()}
    if 'values' in result and isinstance(result['values'], list):
        keys = [key for key, value in result.items() if value == result['values']]
        values = [result['values'] for _ in range(len(keys))]
        random_key = random.choice(keys)
        random_value = random.choice(values)
        result[random_key] = random_value
    return dict_case_random

print(dict_case_random(params))
import json, sys
from collections import defaultdict

params = json.load(sys.stdin)

result = set()
for item in params:
    for word in str(item).split():
        result.add(word)
print(len(result))
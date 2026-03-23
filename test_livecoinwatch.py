#!/usr/bin/env python3
"""Test LiveCoinWatch API with same request as the bot. Reports timing and result."""
import json
import time
import requests

with open("config.json") as f:
    config = json.load(f)
api_key = config.get("livecoinwatch-api-key")
if not api_key:
    print("No livecoinwatch-api-key in config.json")
    exit(1)

url = "https://api.livecoinwatch.com/coins/single"
headers = {
    "content-type": "application/json",
    "x-api-key": api_key,
}
payload = {"currency": "USD", "code": "WJK", "meta": True}

print("Request: POST", url, "code=WJK")
start = time.perf_counter()
try:
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    elapsed = time.perf_counter() - start
    print("Status: %s" % r.status_code)
    print("Elapsed: %.2f s" % elapsed)
    if r.ok:
        data = r.json()
        rate = data.get("rate")
        name = data.get("name", "WJK")
        print("Name: %s" % name)
        print("Rate: %s" % rate)
        if rate is not None:
            print("OK: price $%s" % rate)
        else:
            ath = data.get("allTimeHighUSD")
            print("Rate null; ATH: %s" % ath)
    else:
        print("Body: %s" % (r.text[:500] if r.text else "(empty)"))
except requests.RequestException as e:
    elapsed = time.perf_counter() - start
    print("Elapsed before error: %.2f s" % elapsed)
    print("Error: %s" % e)
except Exception as e:
    print("Error: %s" % e)
    raise

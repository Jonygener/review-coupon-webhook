# app.py
import os
import re
import requests
import random
import string
import json
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
DISCOUNT_ACCESS_TOKEN = os.getenv("DISCOUNT_ACCESS_TOKEN")
SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL")
KLAVIYO_API_KEY = os.getenv("KLAVIYO_API_KEY")
SHOPIFY_API_VERSION = "2023-10"

LOG_FILE = "coupon_log.txt"

app = Flask(__name__)

def shopify_headers(token):
    return {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
    }

def get_customer_by_email(email):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/customers/search.json?query=email:{email}"
    resp = requests.get(url, headers=shopify_headers(SHOPIFY_ACCESS_TOKEN), verify=False)
    resp.raise_for_status()
    data = resp.json()
    if data['customers']:
        return data['customers'][0]
    return None

def update_customer_tags(customer_id, new_tag):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/customers/{customer_id}.json"
    payload = {
        "customer": {
            "id": customer_id,
            "tags": new_tag
        }
    }
    resp = requests.put(url, json=payload, headers=shopify_headers(SHOPIFY_ACCESS_TOKEN), verify=False)
    resp.raise_for_status()
    return resp.json()

def get_variant_id_from_product(product_id):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/products/{product_id}.json"
    resp = requests.get(url, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), verify=False)
    resp.raise_for_status()
    product = resp.json()['product']
    if product['variants']:
        return product['variants'][0]['id']
    else:
        raise ValueError("No variants found for the given product.")

def generate_coupon_code(length=12):
    characters = string.ascii_uppercase + string.digits
    return ''.join(random.choices(characters, k=length))

def log_coupon(email, code, payload):
    with open(LOG_FILE, "a") as f:
        f.write(f"{datetime.utcnow().isoformat()} | {email} | {code} | {payload}\n")

def create_discount_code(email, product_variant_id, product_id):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/price_rules.json"

    discount_code_value = generate_coupon_code()
    price_rule_title = f"Discount_{discount_code_value}"

    price_rule = {
        "price_rule": {
            "title": price_rule_title,
            "target_type": "line_item",
            "target_selection": "entitled",
            "allocation_method": "across",
            "value_type": "percentage",
            "value": -5.0,
            "customer_selection": "all",
            "entitled_variant_ids": [product_variant_id],
            "once_per_customer": True,
            "usage_limit": 1,
            "starts_at": datetime.utcnow().isoformat() + "Z"
        }
    }

    print("DEBUG price_rule payload:")
    print(json.dumps(price_rule, indent=2))

    try:
        price_resp = requests.post(url, json=price_rule, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), verify=False)
        price_resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        return None, f"Error creating price rule: {e}"

    price_rule_id = price_resp.json()['price_rule']['id']

    discount_code_url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/price_rules/{price_rule_id}/discount_codes.json"
    discount_code = {
        "discount_code": {
            "code": discount_code_value
        }
    }

    try:
        discount_resp = requests.post(discount_code_url, json=discount_code, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), verify=False)
        discount_resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        return None, f"Error creating discount code: {e}"

    log_coupon(email, discount_code_value, price_rule_title)

    return discount_resp.json(), discount_code_value

def update_klaviyo_profile(email, discount_code):
    search_url = f"https://a.klaviyo.com/api/profiles/?filter=email%3D{email}"
    headers = {
        "Authorization": f"Klaviyo-API-Key {KLAVIYO_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json",
        "revision": "2024-10-15"
    }

    search_resp = requests.get(search_url, headers=headers, verify=False)
    search_resp.raise_for_status()
    data = search_resp.json()

    if not data.get("data"):
        raise Exception("Profile not found in Klaviyo.")

    profile_id = data["data"][0]["id"]

    patch_url = f"https://a.klaviyo.com/api/profiles/{profile_id}"
    payload = {
        "data": {
            "type": "profile",
            "id": profile_id,
            "attributes": {
                "properties": {
                    "last_review_coupon": discount_code
                }
            }
        }
    }

    print("DEBUG Klaviyo PATCH payload:")
    print(json.dumps(payload, indent=2))

    response = requests.patch(patch_url, headers=headers, json=payload, verify=False)
    print("DEBUG Klaviyo response:")
    print(response.status_code)
    print(response.text)
    response.raise_for_status()

    with open(LOG_FILE, "a") as f:
        f.write(f"{datetime.utcnow().isoformat()} | {email} | Klaviyo profile updated with code {discount_code}\n")

    return response.json()

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    email = data.get("email")
    product_input = data.get("product_id")

    if not email or not product_input:
        return jsonify({"error": "Missing email or product_id"}), 400

    if isinstance(product_input, str):
        match = re.search(r"Product/(\d+)", product_input)
        if match:
            product_id = int(match.group(1))
        else:
            try:
                product_id = int(product_input)
            except ValueError:
                return jsonify({"error": "Invalid product_id format"}), 400
    else:
        product_id = product_input

    customer = get_customer_by_email(email)
    if not customer:
        return jsonify({"error": "Customer not found"}), 404

    existing_tags = customer.get("tags", "")
    review_tag = f"review_{product_id}"

    if review_tag in existing_tags:
        return jsonify({"message": "Tag already exists. Nothing to do."}), 200

    updated_tags = existing_tags + f", {review_tag}" if existing_tags else review_tag
    update_customer_tags(customer["id"], updated_tags)

    variant_id = get_variant_id_from_product(product_id)

    discount_data, discount_code_value = create_discount_code(email, variant_id, product_id)
    if not discount_data:
        return jsonify({"error": discount_code_value}), 500

    update_klaviyo_profile(email, discount_code_value)

    return jsonify({"message": "Tag updated, discount created, profile updated.", "code": discount_code_value}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

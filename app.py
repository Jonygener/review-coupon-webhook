import os
import json
import random
import string
from datetime import datetime
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv

load_dotenv()

SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
DISCOUNT_ACCESS_TOKEN = os.getenv("DISCOUNT_ACCESS_TOKEN")
SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL")
KLAVIYO_API_KEY = os.getenv("KLAVIYO_API_KEY")
SHOPIFY_API_VERSION = "2023-10"

app = Flask(__name__)

def shopify_headers(token):
    return {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token
    }

def generate_discount_code(length=12):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def get_customer_by_email(email):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/customers/search.json?query=email:{email}"
    resp = requests.get(url, headers=shopify_headers(SHOPIFY_ACCESS_TOKEN), verify=False)
    resp.raise_for_status()
    data = resp.json()
    return data['customers'][0] if data['customers'] else None

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
    print("✅ Tag updated")

def get_variant_id_from_product(product_id):
    if product_id.startswith("gid://"):
        product_id = product_id.split("/")[-1]
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/products/{product_id}.json"
    resp = requests.get(url, headers=shopify_headers(SHOPIFY_ACCESS_TOKEN), verify=False)
    resp.raise_for_status()
    product = resp.json()['product']
    print("✅ Variant ID fetched")
    return product['variants'][0]['id'] if product['variants'] else None

def create_discount_code(email, variant_id, product_id):
    discount_code = generate_discount_code()
    price_rule = {
        "price_rule": {
            "title": f"Discount_{discount_code}",
            "target_type": "line_item",
            "target_selection": "entitled",
            "allocation_method": "across",
            "value_type": "percentage",
            "value": -5.0,
            "customer_selection": "all",
            "entitled_variant_ids": [variant_id],
            "once_per_customer": True,
            "usage_limit": 1,
            "starts_at": datetime.utcnow().isoformat() + "Z"
        }
    }
    print("DEBUG price_rule payload:", price_rule)
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/price_rules.json"
    resp = requests.post(url, json=price_rule, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), verify=False)
    resp.raise_for_status()
    price_rule_id = resp.json()['price_rule']['id']

    code_url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/price_rules/{price_rule_id}/discount_codes.json"
    discount_payload = {"discount_code": {"code": discount_code}}
    discount_resp = requests.post(code_url, json=discount_payload, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), verify=False)
    discount_resp.raise_for_status()
    print("✅ Discount created")
    return discount_code

def update_klaviyo_profile(email, discount_code):
    search_url = f"https://a.klaviyo.com/api/profiles/?filter=equals(email,\"{email}\")"
    headers = {
        "Authorization": f"Klaviyo-API-Key {KLAVIYO_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "revision": "2023-10-15"
    }
    search_resp = requests.get(search_url, headers=headers, verify=False)
    search_resp.raise_for_status()
    profile_data = search_resp.json()
    if not profile_data.get("data"):
        print("❌ Klaviyo profile not found")
        return

    profile_id = profile_data["data"][0]["id"]
    update_url = f"https://a.klaviyo.com/api/profiles/{profile_id}"
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
    update_resp = requests.patch(update_url, json=payload, headers=headers, verify=False)
    update_resp.raise_for_status()
    print("✅ Klaviyo updated")

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    email = data.get("email")
    product_id = data.get("product_id")

    if not email or not product_id:
        return jsonify({"error": "Missing email or product_id"}), 400

    try:
        customer = get_customer_by_email(email)
        if not customer:
            return jsonify({"error": "Customer not found"}), 404

        existing_tags = customer.get("tags", "")
        tag = f"review_{product_id.split('/')[-1]}"
        if tag in existing_tags:
            return jsonify({"message": "Tag already exists. Nothing to do."}), 200

        new_tags = f"{existing_tags}, {tag}" if existing_tags else tag
        update_customer_tags(customer["id"], new_tags)

        variant_id = get_variant_id_from_product(product_id)
        if not variant_id:
            return jsonify({"error": "No variant ID found for product"}), 500

        discount_code = create_discount_code(email, variant_id, product_id)
        update_klaviyo_profile(email, discount_code)

        return jsonify({"message": "Coupon assigned", "discount_code": discount_code}), 200

    except requests.HTTPError as e:
        print(f"❌ Exception: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)

import os
import requests
import random
import string
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime

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

def get_customer_by_email(email):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/customers/search.json?query=email:{email}"
    resp = requests.get(url, headers=shopify_headers(SHOPIFY_ACCESS_TOKEN), verify=False)
    resp.raise_for_status()
    customers = resp.json().get("customers", [])
    return customers[0] if customers else None

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

def get_variant_id_from_product(product_gid):
    product_id = product_gid.split("/")[-1]
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/products/{product_id}.json"
    resp = requests.get(url, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), verify=False)
    resp.raise_for_status()
    product = resp.json().get("product", {})
    variants = product.get("variants", [])
    return variants[0]["id"] if variants else None

def generate_discount_code(length=12):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def create_discount_code(email, variant_id):
    discount_code_value = generate_discount_code()
    price_rule_payload = {
        "price_rule": {
            "title": f"Discount_{discount_code_value}",
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
    print("DEBUG price_rule payload:")
    print(price_rule_payload)

    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/price_rules.json"
    price_resp = requests.post(url, json=price_rule_payload, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), verify=False)
    price_resp.raise_for_status()
    price_rule_id = price_resp.json()["price_rule"]["id"]

    discount_code_url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/price_rules/{price_rule_id}/discount_codes.json"
    discount_code_payload = {
        "discount_code": {
            "code": discount_code_value
        }
    }
    discount_resp = requests.post(discount_code_url, json=discount_code_payload, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), verify=False)
    discount_resp.raise_for_status()
    return discount_code_value

def update_klaviyo_profile(email, discount_code):
    search_url = f"https://a.klaviyo.com/api/profiles/?filter=equals(email,\"{email}\")"
    headers = {
        "Authorization": f"Klaviyo-API-Key {KLAVIYO_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json",
        "revision": "2023-10-15"
    }

    search_resp = requests.get(search_url, headers=headers, verify=False)
    search_resp.raise_for_status()
    search_data = search_resp.json()
    if not search_data.get("data"):
        raise ValueError("Profile not found in Klaviyo")
    profile_id = search_data["data"][0]["id"]

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
    print("DEBUG Klaviyo payload:")
    print(payload)
    update_resp = requests.patch(update_url, headers=headers, json=payload, verify=False)
    print("DEBUG Klaviyo response:")
    print(update_resp.status_code)
    print(update_resp.text)
    update_resp.raise_for_status()
    return update_resp.json()

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    email = data.get("email")
    product_gid = data.get("product_id")

    if not email or not product_gid:
        return jsonify({"error": "Missing email or product_id"}), 400

    customer = get_customer_by_email(email)
    if not customer:
        return jsonify({"error": "Customer not found"}), 404

    existing_tags = customer.get("tags", "")
    review_tag = f"review_{product_gid.split('/')[-1]}"
    if review_tag in existing_tags:
        return jsonify({"message": "Tag already exists. Nothing to do."}), 200

    updated_tags = existing_tags + f", {review_tag}" if existing_tags else review_tag
    update_customer_tags(customer["id"], updated_tags)

    variant_id = get_variant_id_from_product(product_gid)
    if not variant_id:
        return jsonify({"error": "Variant not found for the given product"}), 400

    discount_code_value = create_discount_code(email, variant_id)
    update_klaviyo_profile(email, discount_code_value)

    return jsonify({"message": "Success", "discount_code": discount_code_value}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

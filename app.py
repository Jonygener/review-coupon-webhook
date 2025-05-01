import os
import requests
import logging
import random
import string
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Setup logging
logging.basicConfig(filename="webhook.log", level=logging.DEBUG)

SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
DISCOUNT_ACCESS_TOKEN = os.getenv("DISCOUNT_ACCESS_TOKEN")
KLAVIYO_API_KEY = os.getenv("KLAVIYO_API_KEY")
SHOPIFY_API_VERSION = "2023-10"


def shopify_headers(token):
    return {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
    }

def get_customer_by_email(email):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/customers/search.json?query=email:{email}"
    resp = requests.get(url, headers=shopify_headers(SHOPIFY_ACCESS_TOKEN), verify=False)
    resp.raise_for_status()
    customers = resp.json().get("customers", [])
    return customers[0] if customers else None

def update_customer_tags(customer_id, tags):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/customers/{customer_id}.json"
    payload = {"customer": {"id": customer_id, "tags": tags}}
    resp = requests.put(url, headers=shopify_headers(SHOPIFY_ACCESS_TOKEN), json=payload, verify=False)
    resp.raise_for_status()
    return resp.json()

def get_variant_id_from_product(product_id):
    # Using SHOPIFY_ACCESS_TOKEN for better permissions
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/products/{product_id}.json"
    resp = requests.get(url, headers=shopify_headers(SHOPIFY_ACCESS_TOKEN), verify=False)
    resp.raise_for_status()
    product = resp.json().get("product", {})
    variants = product.get("variants", [])
    return variants[0].get("id") if variants else None

def generate_coupon_code(length=12):
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choices(chars, k=length))

def create_discount_code(variant_id):
    coupon_code = generate_coupon_code()
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/price_rules.json"
    price_rule = {
        "price_rule": {
            "title": f"Discount_{coupon_code}",
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
    logging.debug("price_rule payload: %s", price_rule)
    resp = requests.post(url, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), json=price_rule, verify=False)
    resp.raise_for_status()
    rule_id = resp.json()["price_rule"]["id"]

    code_url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/price_rules/{rule_id}/discount_codes.json"
    code_payload = {"discount_code": {"code": coupon_code}}
    code_resp = requests.post(code_url, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), json=code_payload, verify=False)
    code_resp.raise_for_status()
    return coupon_code

def update_klaviyo_profile(email, discount_code):
    # Search profile by email
    search_url = f"https://a.klaviyo.com/api/profiles/?filter=email%3D{email}"
    headers = {
        "Authorization": f"Klaviyo-API-Key {KLAVIYO_API_KEY}",
        "Content-Type": "application/json",
        "revision": "2023-10-15",
    }
    search_resp = requests.get(search_url, headers=headers, verify=False)
    search_resp.raise_for_status()
    data = search_resp.json()
    profile_id = data["data"][0]["id"]

    # Patch profile
    patch_url = f"https://a.klaviyo.com/api/profiles/{profile_id}/"
    patch_payload = {
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
    patch_resp = requests.patch(patch_url, headers=headers, json=patch_payload, verify=False)
    patch_resp.raise_for_status()
    return patch_resp.json()

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        email = data.get("email")
        product_id = data.get("product_id")

        if not email or not product_id:
            return jsonify({"error": "Missing email or product_id"}), 400

        if isinstance(product_id, str) and product_id.startswith("gid://"):
            product_id = product_id.split("/")[-1]

        customer = get_customer_by_email(email)
        if not customer:
            return jsonify({"error": "Customer not found"}), 404

        tag = f"review_{product_id}"
        existing_tags = customer.get("tags", "")

        if tag in existing_tags:
            return jsonify({"message": "Tag already exists. Skipping."}), 200

        new_tags = f"{existing_tags}, {tag}" if existing_tags else tag
        update_customer_tags(customer["id"], new_tags)

        variant_id = get_variant_id_from_product(product_id)
        if not variant_id:
            return jsonify({"error": "Variant not found"}), 404

        discount_code = create_discount_code(variant_id)
        update_klaviyo_profile(email, discount_code)

        return jsonify({"message": "Coupon created and saved."}), 200

    except Exception as e:
        logging.exception("ERROR in /webhook")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

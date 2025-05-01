import os
import requests
import logging
import random
import string
from datetime import datetime
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL")
SHOPIFY_API_VERSION = "2023-10"
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
DISCOUNT_ACCESS_TOKEN = os.getenv("DISCOUNT_ACCESS_TOKEN")
KLAVIYO_API_KEY = os.getenv("KLAVIYO_API_KEY")

app = Flask(__name__)

logging.basicConfig(filename='log.txt', level=logging.DEBUG)

def shopify_headers(token):
    return {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
    }

def generate_discount_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))

def get_customer_by_email(email):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/customers/search.json?query=email:{email}"
    resp = requests.get(url, headers=shopify_headers(SHOPIFY_ACCESS_TOKEN), verify=False)
    resp.raise_for_status()
    customers = resp.json().get("customers", [])
    return customers[0] if customers else None

def update_customer_tags(customer_id, new_tags):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/customers/{customer_id}.json"
    payload = {
        "customer": {
            "id": customer_id,
            "tags": new_tags
        }
    }
    resp = requests.put(url, json=payload, headers=shopify_headers(SHOPIFY_ACCESS_TOKEN), verify=False)
    resp.raise_for_status()

def get_variant_id_from_product(product_id):
    if isinstance(product_id, str) and product_id.startswith("gid://"):
        product_id = product_id.split("/")[-1]
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/products/{product_id}.json"
    resp = requests.get(url, headers=shopify_headers(SHOPIFY_ACCESS_TOKEN), verify=False)
    resp.raise_for_status()
    product = resp.json()["product"]
    return product["variants"][0]["id"]

def create_discount_code(email, variant_id, code):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/price_rules.json"
    payload = {
        "price_rule": {
            "title": f"Discount_{code}",
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
    logging.debug("price_rule payload: %s", payload)
    print("DEBUG price_rule payload:", payload, flush=True)
    resp = requests.post(url, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), json=payload, verify=False)
    resp.raise_for_status()
    price_rule_id = resp.json()['price_rule']['id']

    discount_url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/price_rules/{price_rule_id}/discount_codes.json"
    discount_code = {
        "discount_code": {"code": code}
    }
    resp = requests.post(discount_url, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), json=discount_code, verify=False)
    resp.raise_for_status()
    return code

def update_klaviyo_profile(email, discount_code):
    search_url = f"https://a.klaviyo.com/api/profiles/?filter=email%3D{email}"
    search_headers = {
        "Authorization": f"Klaviyo-API-Key {KLAVIYO_API_KEY}",
        "accept": "application/json",
        "revision": "2023-10-15"
    }
    search_resp = requests.get(search_url, headers=search_headers, verify=False)
    search_resp.raise_for_status()
    profile_id = search_resp.json()["data"][0]["id"]

    update_url = f"https://a.klaviyo.com/api/profiles/{profile_id}"
    update_headers = {
        "Authorization": f"Klaviyo-API-Key {KLAVIYO_API_KEY}",
        "accept": "application/json",
        "revision": "2023-10-15",
        "Content-Type": "application/json"
    }
    payload = {
        "data": {
            "type": "profile",
            "id": profile_id,
            "attributes": {
                "properties": {
                    "coupon_assigned": discount_code
                }
            }
        }
    }
    resp = requests.patch(update_url, headers=update_headers, json=payload, verify=False)
    resp.raise_for_status()

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        email = data.get("email")
        product_id = data.get("product_id")

        if not email or not product_id:
            return jsonify({"error": "Missing email or product_id"}), 400

        customer = get_customer_by_email(email)
        if not customer:
            return jsonify({"error": "Customer not found"}), 404

        existing_tags = customer.get("tags", "")
        review_tag = f"review_{product_id.split('/')[-1]}"
        if review_tag in existing_tags:
            return jsonify({"message": "Tag already exists. Nothing to do."}), 200

        updated_tags = f"{existing_tags}, {review_tag}" if existing_tags else review_tag
        update_customer_tags(customer["id"], updated_tags)
        print("✅ Tag updated", flush=True)

        variant_id = get_variant_id_from_product(product_id)
        print("✅ Variant ID fetched", flush=True)

        discount_code = generate_discount_code()
        create_discount_code(email, variant_id, discount_code)
        print("✅ Discount created", flush=True)

        update_klaviyo_profile(email, discount_code)
        print("✅ Klaviyo updated", flush=True)

        return jsonify({"message": "Coupon created and assigned."}), 200

    except Exception as e:
        logging.error("ERROR in /webhook: %s", str(e))
        print(f"❌ Exception: {e}", flush=True)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

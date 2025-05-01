import os
import re
import requests
import logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime
import random
import string

load_dotenv()

SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
DISCOUNT_ACCESS_TOKEN = os.getenv("DISCOUNT_ACCESS_TOKEN")
SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL")
KLAVIYO_API_KEY = os.getenv("KLAVIYO_API_KEY")
SHOPIFY_API_VERSION = "2023-10"

app = Flask(__name__)

logging.basicConfig(filename='webhook.log', level=logging.DEBUG, format='%(asctime)s %(message)s')

def shopify_headers(token):
    return {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
    }

def extract_product_id(gid):
    match = re.search(r"Product/(\d+)$", gid)
    return match.group(1) if match else None

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
    return resp.json()

def get_variant_id_from_product(product_id):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/products/{product_id}.json"
    resp = requests.get(url, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), verify=False)
    resp.raise_for_status()
    product = resp.json()['product']
    if product['variants']:
        return product['variants'][0]['id']
    raise ValueError("No variants found for the given product.")

def generate_random_code(length=12):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def create_discount_code(email, product_variant_id, product_id):
    discount_code_value = generate_random_code()
    price_rule = {
        "price_rule": {
            "title": f"Discount_{discount_code_value}",
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

    logging.debug("DEBUG price_rule payload:\n%s", price_rule)

    price_resp = requests.post(
        f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/price_rules.json",
        json=price_rule,
        headers=shopify_headers(DISCOUNT_ACCESS_TOKEN),
        verify=False
    )
    price_resp.raise_for_status()
    price_rule_id = price_resp.json()['price_rule']['id']

    discount_code = {
        "discount_code": {
            "code": discount_code_value
        }
    }

    discount_resp = requests.post(
        f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/price_rules/{price_rule_id}/discount_codes.json",
        json=discount_code,
        headers=shopify_headers(DISCOUNT_ACCESS_TOKEN),
        verify=False
    )
    discount_resp.raise_for_status()
    return discount_resp.json(), discount_code_value

def update_klaviyo_profile(email, discount_code):
    url = "https://a.klaviyo.com/api/profiles/"
    headers = {
        "Authorization": f"Klaviyo-API-Key {KLAVIYO_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json",
        "revision": "2023-10-15"
    }
    payload = {
        "data": {
            "type": "profile",
            "attributes": {
                "email": email,
                "properties": {
                    "coupon_assigned": True,
                    "last_review_coupon": discount_code
                }
            }
        }
    }

    logging.debug("DEBUG Klaviyo payload:\n%s", payload)

    response = requests.post(url, headers=headers, json=payload, verify=False)

    logging.debug("DEBUG Klaviyo response:\n%s\n%s", response.status_code, response.text)

    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if response.status_code == 409:
            logging.info("Klaviyo profile already exists – skipping creation.")
        else:
            raise e
    return response.status_code

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    email = data.get("email")
    gid = data.get("product_id")
    product_id = extract_product_id(gid)

    if not email or not product_id:
        return jsonify({"error": "Missing email or product_id"}), 400

    customer = get_customer_by_email(email)
    if not customer:
        return jsonify({"error": "Customer not found"}), 404

    existing_tags = customer.get("tags", "")
    review_tag = f"review_{product_id}"

    if review_tag in existing_tags.split(","):
        return jsonify({"message": "Tag already exists. Nothing to do."}), 200

    updated_tags = existing_tags + f", {review_tag}" if existing_tags else review_tag
    update_customer_tags(customer["id"], updated_tags)

    variant_id = get_variant_id_from_product(product_id)
    discount_data, discount_code_value = create_discount_code(email, variant_id, product_id)

    update_klaviyo_profile(email, discount_code_value)

    return jsonify({"message": "Tag, discount, and Klaviyo update complete."}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

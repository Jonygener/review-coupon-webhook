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


def log_debug(msg):
    with open("log.txt", "a") as log_file:
        log_file.write(msg + "\n")


def shopify_headers(token):
    return {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
    }


def get_customer_by_email(email):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/customers/search.json?query=email:{email}"
    resp = requests.get(url, headers=shopify_headers(SHOPIFY_ACCESS_TOKEN), verify=False)
    log_debug(f"Customer search URL: {url}")
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
    log_debug(f"Updating customer tags with payload: {payload}")
    resp = requests.put(url, json=payload, headers=shopify_headers(SHOPIFY_ACCESS_TOKEN), verify=False)
    resp.raise_for_status()
    return resp.json()


def get_variant_id_from_product(product_id):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/products/{product_id}.json"
    resp = requests.get(url, headers=shopify_headers(SHOPIFY_ACCESS_TOKEN), verify=False)
    log_debug(f"Getting variant for product ID: {product_id} from URL: {url}")
    resp.raise_for_status()
    product = resp.json()['product']
    return product['variants'][0]['id'] if product['variants'] else None


def generate_discount_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))


def create_discount_code(email, variant_id, product_id):
    discount_code_value = generate_discount_code()
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/price_rules.json"

    price_rule = {
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

    log_debug(f"DEBUG price_rule payload: {price_rule}")
    price_resp = requests.post(url, json=price_rule, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), verify=False)
    price_resp.raise_for_status()
    price_rule_id = price_resp.json()['price_rule']['id']

    discount_code_url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/price_rules/{price_rule_id}/discount_codes.json"
    discount_code = {"discount_code": {"code": discount_code_value}}

    discount_resp = requests.post(discount_code_url, json=discount_code, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), verify=False)
    discount_resp.raise_for_status()

    return discount_code_value


def update_klaviyo_profile(email, discount_code):
    search_url = f"https://a.klaviyo.com/api/profiles/?filter=email={email}"
    headers = {
        "Authorization": f"Klaviyo-API-Key {KLAVIYO_API_KEY}",
        "Revision": "2023-10-15",
        "accept": "application/json",
    }
    search_resp = requests.get(search_url, headers=headers, verify=False)
    log_debug(f"Searching Klaviyo profile for {email}: {search_url}")
    search_resp.raise_for_status()
    profiles = search_resp.json().get("data", [])
    if not profiles:
        log_debug(f"Klaviyo profile not found for {email}")
        return
    profile_id = profiles[0]["id"]

    url = f"https://a.klaviyo.com/api/profiles/{profile_id}/"
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
    log_debug(f"Updating Klaviyo profile with: {payload}")
    response = requests.patch(url, headers=headers, json=payload, verify=False)
    response.raise_for_status()


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        email = data.get("email")
        product_id = data.get("product_id")

        log_debug(f"Received webhook with email={email}, product_id={product_id}")

        if not email or not product_id:
            return jsonify({"error": "Missing email or product_id"}), 400

        customer = get_customer_by_email(email)
        if not customer:
            return jsonify({"error": "Customer not found"}), 404

        existing_tags = customer.get("tags", "")
        review_tag = f"review_{product_id}"

        if review_tag in existing_tags:
            log_debug("Tag already exists. Ending process early.")
            return jsonify({"message": "Tag already exists. Nothing to do."}), 200

        updated_tags = existing_tags + f", {review_tag}" if existing_tags else review_tag
        update_customer_tags(customer["id"], updated_tags)

        variant_id = get_variant_id_from_product(product_id)
        discount_code = create_discount_code(email, variant_id, product_id)
        update_klaviyo_profile(email, discount_code)

        return jsonify({"message": "Tag added, discount created, and Klaviyo profile updated."}), 200
    except Exception as e:
        log_debug(f"ERROR in /webhook: {str(e)}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

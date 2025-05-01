import os
import requests
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
    # Convert GID to plain numeric ID if needed
    if isinstance(product_id, str) and product_id.startswith("gid://"):
        product_id = product_id.split("/")[-1]
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/products/{product_id}.json"
    resp = requests.get(url, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), verify=False)
    resp.raise_for_status()
    product = resp.json()['product']
    if product['variants']:
        return product['variants'][0]['id']
    else:
        raise ValueError("No variants found for the given product.")


def generate_coupon_code(length=12):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


def create_discount_code(email, product_variant_id):
    coupon_code = generate_coupon_code()
    price_rule = {
        "price_rule": {
            "title": f"Discount_{coupon_code}",
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

    url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/price_rules.json"
    price_resp = requests.post(url, json=price_rule, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), verify=False)
    price_resp.raise_for_status()
    price_rule_id = price_resp.json()['price_rule']['id']

    discount_code_url = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}/price_rules/{price_rule_id}/discount_codes.json"
    discount_code = {
        "discount_code": {
            "code": coupon_code
        }
    }
    discount_resp = requests.post(discount_code_url, json=discount_code, headers=shopify_headers(DISCOUNT_ACCESS_TOKEN), verify=False)
    discount_resp.raise_for_status()
    return coupon_code


def update_klaviyo_profile(email, discount_code):
    search_url = f"https://a.klaviyo.com/api/profiles/?filter=email%3D{email}"
    headers = {
        "Authorization": f"Klaviyo-API-Key {KLAVIYO_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json",
        "revision": "2023-10-15"
    }
    search_resp = requests.get(search_url, headers=headers, verify=False)
    search_resp.raise_for_status()
    profiles = search_resp.json().get("data", [])
    if not profiles:
        raise ValueError("No Klaviyo profile found for this email")

    profile_id = profiles[0]['id']
    update_url = f"https://a.klaviyo.com/api/profiles/{profile_id}/"
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
    response = requests.patch(update_url, headers=headers, json=payload, verify=False)
    response.raise_for_status()
    return response.json()


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    email = data.get("email")
    product_id = data.get("product_id")

    if not email or not product_id:
        return jsonify({"error": "Missing email or product_id"}), 400

    customer = get_customer_by_email(email)
    if not customer:
        return jsonify({"error": "Customer not found"}), 404

    existing_tags = customer.get("tags", "")
    review_tag = f"review_{product_id}"

    if review_tag in existing_tags:
        return jsonify({"message": "Tag already exists", "coupon_assigned": False}), 200

    updated_tags = existing_tags + f", {review_tag}" if existing_tags else review_tag
    update_customer_tags(customer["id"], updated_tags)

    variant_id = get_variant_id_from_product(product_id)
    discount_code_value = create_discount_code(email, variant_id)
    update_klaviyo_profile(email, discount_code_value)

    return jsonify({"message": "Coupon created", "coupon_code": discount_code_value, "coupon_assigned": True}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

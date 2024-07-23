from fastapi import FastAPI, Form
from playwright.async_api import async_playwright, Browser, TimeoutError
from pydantic import BaseModel
from typing import Dict
import redis.asyncio as redis
from ast import literal_eval
import json
import base64
import traceback
import uuid
import httpx
import re
import os
from dotenv import load_dotenv
load_dotenv()

app = FastAPI()

# Global variables
playwright_instance = None
browser: Browser = None
redis_client = None

states_dict = literal_eval(os.getenv("STATES"))

# Session storage
class Session(BaseModel):
    headers: Dict[str, str]
    cookies: Dict[str, str] = {}
    cart_id: str = None


@app.on_event("startup")
async def startup_event():
    """Initialize the Playwright instance and Redis client on startup."""
    global playwright_instance, browser, redis_client
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(headless=True)
    redis_client = redis.from_url(os.getenv("REDIS_CONN"))


@app.on_event("shutdown")
async def shutdown_event():
    """Close the Playwright instance and Redis client on shutdown."""
    global playwright_instance, browser, redis_client
    await redis_client.close()
    await browser.close()
    await playwright_instance.stop()


async def save_session(context, session_id):
    """Save the browser session state to Redis."""
    global redis_client
    storage = await context.storage_state()
    encoded_storage = base64.b64encode(json.dumps(storage).encode()).decode()
    await redis_client.set(f"session:{session_id}", encoded_storage)


async def load_session(session_id):
    """Load the browser session state from Redis."""
    global browser, redis_client
    encoded_storage = await redis_client.get(f"session:{session_id}")
    if encoded_storage:
        storage = json.loads(base64.b64decode(encoded_storage).decode())
        context = await browser.new_context(storage_state=storage)
        return context
    return None


async def save_hoodie_product_data(session_id, hoodie_prod_data):
    """Save the Hoodie product data to Redis."""
    global redis_client
    encoded_data = base64.b64encode(json.dumps(hoodie_prod_data).encode()).decode()
    await redis_client.set(f"hoodie_prod_data:{session_id}", encoded_data)


async def load_hoodie_product_data(session_id):
    """Load the Hoodie product data from Redis."""
    global redis_client
    encoded_data = await redis_client.get(f"hoodie_prod_data:{session_id}")
    if encoded_data:
        hoodie_prod_data = json.loads(base64.b64decode(encoded_data).decode())
        return hoodie_prod_data


async def save_submit_form(session_id, submit_form):
    """Save the submit form data to Redis."""
    global redis_client
    encoded_data = base64.b64encode(json.dumps(submit_form).encode()).decode()
    await redis_client.set(f"submit_form:{session_id}", encoded_data)


async def load_submit_form(session_id):
    """Load the submit form data from Redis."""
    global redis_client
    encoded_data = await redis_client.get(f"submit_form:{session_id}")
    if encoded_data:
        submit_form = json.loads(base64.b64decode(encoded_data).decode())
        return submit_form


async def add_to_cart(page, quantity):
    """Add a product to the cart."""
    age_rstr_yes_btn = 'button[data-testid="age-restriction-yes"]'
    if await page.locator(age_rstr_yes_btn).is_visible():
        await page.locator(age_rstr_yes_btn).click()

    await page.wait_for_selector(
        'div[data-testid="quantity-select"]', state="visible", timeout=10000
    )
    await page.click('div[data-testid="quantity-select"]')
    await page.wait_for_selector('ul[role="listbox"]')

    quantity_options = await page.query_selector_all(
        'li[data-testid="quantity-select-option"]'
    )
    available_quantities = [
        int(await option.get_attribute("data-value")) for option in quantity_options
    ]

    if quantity in available_quantities:
        await page.click(
            f'li[data-testid="quantity-select-option"][data-value="{quantity}"]'
        )
        await page.wait_for_function(
            f'document.querySelector(\'div[data-testid="quantity-select"]\').textContent === "{quantity}"'
        )
    else:
        return {"success": False, "error": f"max quantity {available_quantities[-1]}"}

    add_to_cart_button = await page.query_selector(
        'button[class*="StyledAddToCartButton"]'
    )
    if add_to_cart_button:
        await add_to_cart_button.click()
        closed_modal_selector = (
            'div[data-test="closed-but-modal"] button:has-text("Continue")'
        )
        if await page.locator(closed_modal_selector).is_visible():
            await page.locator(closed_modal_selector).click()

        purchase_limit_selector = (
            'div[data-testid="ernie-container"] span[data-testid="ernie-message"]'
        )
        if await page.locator(purchase_limit_selector).is_visible():
            purchase_limit_message = await page.inner_text(purchase_limit_selector)
            return {"success": False, "error": purchase_limit_message}
        return {"success": True}


async def extract_id_and_query(url):
    """Extract the product ID and query from the URL."""
    pattern = r"/products/_(\d+)(/.*)"
    match = re.search(pattern, url)
    if match:
        id = match.group(1)
        query = match.group(2).lstrip("/")
        return id, query
    else:
        return None, None


async def get_hoodie_product_data(prod_url):
    """Fetch product data from the Hoodie API."""
    base_url = "https://www.askhoodie.com/api/search"
    cm_id, query = await extract_id_and_query(prod_url)

    payload = json.dumps(
        {
            "embedToken": "",
            "method": "search",
            "args": [
                [
                    {
                        "query": query,
                        "indexName": "all_PRODUCTS_V2",
                        "params": {
                            "filters": f"IN_STOCK: true AND CM_ID: {cm_id}",
                            "clickAnalytics": True,
                            "aroundLatLng": "41.88266336542182,-87.62333152985933",
                            "getRankingInfo": True,
                            "aroundRadius": 800000,
                            "hitsPerPage": 10,
                        },
                    }
                ]
            ],
        }
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Referer": prod_url,
        "Content-Type": "application/json",
        "Origin": "https://www.askhoodie.com",
        "DNT": "1",
        "Connection": "keep-alive",
        "Cookie": "currentLocation=%7B%22mapPosition%22%3A%7B%22lat%22%3A41.88266336542182%2C%22lng%22%3A-87.62333152985933%7D%2C%22short_address%22%3A%22Chicago%2C%20IL%22%2C%22radius%22%3A20%2C%22locatorType%22%3A%22StoresNProducts%22%7D",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Priority": "u=4",
        "TE": "trailers",
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(base_url, headers=headers, data=payload)
        if response.status_code == 200:
            raw_data = response.json()
            hits = raw_data["results"][0]["hits"]
            dutchie_data = next(
                (i for i in hits if i["URL"].startswith("https://dutchie.com")), None
            )
            if dutchie_data:
                return {
                    "product_url": dutchie_data["URL"],
                    "product_name": dutchie_data["NAME"],
                    "cm_id": dutchie_data["CM_ID"],
                    "object_id": dutchie_data["objectID"],
                    "variant_id": dutchie_data["VARIANTS"][0]["VARIANT_ID"],
                    "master_d_id": dutchie_data["MASTER_D_ID"],
                    "master_d_name": dutchie_data["MASTER_D_NAME"],
                }
        print(f"Status:{response.status_code}\nDetail:{response.content}")
        return None


async def proceed_to_checkout(page, user_info):
    """Proceed to the checkout page."""
    global states_dict
    try:
        await page.get_by_test_id("cartButton").click()
        if user_info.get("quantity"):
            await page.get_by_test_id("cart-item-container").get_by_test_id("quantity-select").click()
            await page.get_by_text(str(user_info["quantity"]), exact=True).click()
        
        purchase_limit_selector = (
            'div[data-testid="ernie-container"] span[data-testid="ernie-message"]'
        )
        if await page.locator(purchase_limit_selector).is_visible():
            purchase_limit_message = await page.inner_text(purchase_limit_selector)
            return {"success": False, "error": purchase_limit_message}
        
        await page.get_by_test_id("cart-checkout-button").click()
        
        return {"success": True}
    except Exception as e:
        print(f"Exception during checkout: {e}\nTraceback: {traceback.format_exc()}")
        return {"success": False, "error": str(e)}


async def submit_order(page, user_info):
    try:
        # await page.get_by_label("First Name *").click()
        await page.get_by_label("First Name *").fill(user_info["first_name"])
        # await page.get_by_label("Last Name *").click()
        await page.get_by_label("Last Name *").fill(user_info["last_name"])
        # await page.get_by_placeholder("(___) ___-____").click()
        await page.get_by_placeholder("(___) ___-____").fill(user_info["mobile_phone"])
        # await page.get_by_test_id("guest-customer-section").get_by_placeholder("mm/dd/yyyy").click()
        await page.get_by_test_id("guest-customer-section").get_by_placeholder("mm/dd/yyyy").fill(user_info["birthdate"])
        # await page.get_by_label("Email *").click()
        await page.get_by_label("Email *").fill(user_info["email"])
        
        try:
            await page.wait_for_selector('div:has-text("Connect to Rewards")', timeout=5000)  # 5 seconds timeout
            print("'Connect to Rewards' element has appeared.")
        except TimeoutError:
            print("'Connect to Rewards' element did not appear within 5 seconds. Moving forward.")
            pass
        
        await page.get_by_label("Which state do you live in?").select_option(states_dict[user_info["state"].lower()])
        
        if user_info.get("promo_code"):
            # Click the "Add a promo code" button
            await page.click('button.link__StyledButton-hbyqoc-1:has-text("Add a promo code")')
            
            # Wait for the promo code input to be visible
            await page.wait_for_selector('input[placeholder="Enter promo code"]', state='visible', timeout=5000)
            
            # Fill the promo code
            await page.fill('input[placeholder="Enter promo code"]', user_info["promo_code"])
            
            # Click the "Apply" button
            await page.click('button:has-text("Apply")')
            
            # Wait for potential error message
            try:
                promo_error = await page.wait_for_selector('text="Sorry, this promo code is"', timeout=5000)
                if promo_error:
                    return {"success": False, "error": "Sorry, this promo code is invalid"}
            except TimeoutError:
                # No error message appeared, assume promo code was applied successfully
                pass

        # Extract subtotal
        subtotal = await page.locator('div.cost-table__CostItem-ggum6p-2:has-text("Subtotal:") + div').inner_text()

        # Extract taxes
        taxes = await page.locator('div.cost-table__CostItem-ggum6p-2:has-text("Taxes:") + div').inner_text()

        # Extract order total
        order_total = await page.locator('[data-testid="order-total"]').inner_text()

        # Extract pickup time
        pickup_time = await page.locator('div.sc-AxjAm.btdUwz').inner_text()
        
        await page.locator("[data-test=\"place-order\"]").click()
        await page.get_by_test_id("guest-checkout-success-header").click()

        result = {
            "success": True,
            "subtotal": subtotal,
            "taxes": taxes,
            "order_total": order_total,
            "pickup_time": pickup_time
        }

        return result
    except Exception as e:
        print(f"Exception during checkout: {e}\nTraceback: {traceback.format_exc()}")
        return {"success": False, "error": str(e)}


@app.post("/add_to_cart")
async def api_add_to_cart(
    product_url: str = Form(...), quantity: int = Form(...)):
    """
    Add a product to the cart.

    Args:
        product_url (str): The URL of the product.
        quantity (int): The quantity of the product to add to the cart.

    Returns:
        JSONResponse: The response containing the status and details of the cart addition.
    """
    global browser
    try:
        hoodie_prod_data = await get_hoodie_product_data(product_url)
        if not hoodie_prod_data:
            return {"status": "error", "message": "No dutchie link found for the url"}
        
        add_cart_response = None
        context = await browser.new_context()
        page = await context.new_page()
        
        async def intercept_response(response):
            nonlocal add_cart_response
            if response.request.post_data:
                if (
                    response.url.startswith("https://dutchie.com/graphql")
                    and "PersistCheckoutV2" in response.request.post_data
                ):
                    try:
                        add_cart_response = await response.json()
                    except json.JSONDecodeError:
                        print("Failed to parse cart response JSON")

        # await page.route("**/*", intercept_request)
        page.on("response", intercept_response)

        await page.goto(hoodie_prod_data["product_url"])
        await page.wait_for_load_state("domcontentloaded")

        status = await add_to_cart(page, quantity)
        session_id = str(uuid.uuid4())
        await page.wait_for_timeout(4000)

        await save_session(context, session_id)
        await save_hoodie_product_data(session_id, hoodie_prod_data)
        await page.close()
        await context.close()
        if status["success"]:
            response = {
                "status": "success",
                "session_id": session_id,
                "added_quantity": quantity,
                "product_name":hoodie_prod_data["product_name"],
                "cm_id":hoodie_prod_data["cm_id"],
                "variant_id":hoodie_prod_data["variant_id"],
                "dispensary_name":hoodie_prod_data["master_d_name"],
                "cart_id": (
                    add_cart_response["data"]["persistCheckoutV2"]["checkoutToken"]
                    if add_cart_response
                    else None
                ),
            }
            return response
        else:
            return {"status": "error", "message": status["error"]}
    except Exception as e:
        print(f"Exception: {e}\nTraceback: {traceback.format_exc()}")
        return {"status": "error", "message": str(e)}


@app.post("/proceed_to_checkout")
async def checkout(
    session_id: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    mobile_phone: str = Form(...),
    birthdate: str = Form(...),
    email: str = Form(...),
    state: str = Form(...),
    promo_code: str = Form(None),
    pickup_option: str = Form(...),
    quantity: int = Form(None),
    medical_card_number: str = Form(None),
    medical_card_expiration: str = Form(None),
    medical_card_state: str = Form(None),
):
    """
    Proceed to the checkout page.

    Args:
        session_id (str): The session ID.
        first_name (str): The first name of the user.
        last_name (str): The last name of the user.
        mobile_phone (str): The mobile phone number of the user.
        birthdate (str): The birthdate of the user.
        email (str): The email address of the user.
        state (str): The state of the user.
        promo_code (str, optional): The promo code. Defaults to None.
        pickup_option (str): The pickup option.
        quantity (int, optional): The quantity of the product. Defaults to None.
        medical_card_number (str, optional): The medical card number. Defaults to None.
        medical_card_expiration (str, optional): The medical card expiration date. Defaults to None.
        medical_card_state (str, optional): The medical card state. Defaults to None.

    Returns:
        JSONResponse: The response containing the status and details of the checkout process.
    """
    user_info = {
        "first_name": first_name,
        "last_name": last_name,
        "mobile_phone": mobile_phone,
        "birthdate": birthdate,
        "email": email,
        "state": state,
        "promo_code": promo_code,
        "pickup_option": pickup_option,
        "quantity": quantity,
        "medical_card_number": medical_card_number,
        "medical_card_expiration": medical_card_expiration,
        "medical_card_state": medical_card_state,
    }
    global browser
    try:
        context = await load_session(session_id)
        if not context:
            return {"status": "error", "message": "Session not found"}

        hoodie_product_data = await load_hoodie_product_data(session_id)
        
        page = await context.new_page()
        await page.goto(hoodie_product_data["product_url"])
        await page.wait_for_load_state("domcontentloaded")

        status = await proceed_to_checkout(page, user_info)

        await save_session(context, session_id)
        await save_submit_form(session_id, user_info)
        await page.close()
        await context.close()
        if status["success"]:
            return {
                "status": "success",
                "session_id":session_id,
                "product_name":hoodie_product_data["product_name"],
                "cm_id":hoodie_product_data["cm_id"],
                "variant_id":hoodie_product_data["variant_id"],
                "dispensary_name":hoodie_product_data["master_d_name"],
            }
        else:
            return {"status": "error", "message": status["error"]}
    except Exception as e:
        print(f"Exception: {e}\nTraceback: {traceback.format_exc()}")
        return {"status": "error", "message": str(e)}


@app.post("/submit_order")
async def api_submit_order(
    session_id: str = Form(...),
):
    """
    Submit the order.

    Args:
        session_id (str): The session ID.

    Returns:
        JSONResponse: The response containing the status and details of the order submission.
    """
    global browser
    try:
        context = await load_session(session_id)
        if not context:
            return {"status": "error", "message": "Session not found"}

        hoodie_product_data = await load_hoodie_product_data(session_id)
        user_info = await load_submit_form(session_id)
        
        page = await context.new_page()
        await page.goto("https://dutchie.com/checkout")
        await page.wait_for_load_state("domcontentloaded")

        status = await submit_order(page, user_info)
        await save_session(context, session_id)
        await page.close()
        await context.close()
        if status["success"]:
            return {
                "status": "success",
                "message": "Order submitted successfully!",
                "order_details": {
                    "product_name":hoodie_product_data["product_name"],
                    "cm_id":hoodie_product_data["cm_id"],
                    "variant_id":hoodie_product_data["variant_id"],
                    # "order_id": order_id,
                    "pickup_time": status["pickup_time"],
                    # "pickup_instructions": pickup_instructions,
                    "subtotal": status["subtotal"],
                    "taxes": status["taxes"],
                    "order_total": status["order_total"],
                    "dispensary_name":hoodie_product_data["master_d_name"],
                }
            }
        else:
            return {"status": "error", "message": status["error"]}
    except Exception as e:
        print(f"Exception during order submission: {e}\nTraceback: {traceback.format_exc()}")
        return {"status": "error", "message": str(e)}

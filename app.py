from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from PIL import Image
from pydantic import ValidationError
from google import genai
import re
import json
import os
from werkzeug.middleware.proxy_fix import ProxyFix
import io
from ocr_module import sample_ocr_image_data
from models import ReceiptSummary, SpendingRule
import expense_agent
import storage

# === Configure Gemini ===
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# === Flask App ===
app = Flask(__name__)
allowed_origins = [
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]
cors_origins = "*" if "*" in allowed_origins else allowed_origins
CORS(
    app,
    resources={r"/*": {"origins": cors_origins}},
    allow_headers="*",
    methods=["GET", "POST", "DELETE", "OPTIONS"],
)
storage.init_db()
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)


def get_client_ip():
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.remote_addr or "unknown"


limiter = Limiter(get_client_ip, app=app, default_limits=["60 per minute"])


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if "*" in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = "*"
    elif origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, Authorization, X-User-Id")
    response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
    return response

def fix_image_orientation(image: Image.Image) -> Image.Image:
    try:
        exif = image._getexif()
        if exif is not None:
            orientation_value = exif.get(274, 1)

            # Apply necessary rotation
            if orientation_value == 3:
                image = image.rotate(180, expand=True)
            elif orientation_value == 6:
                image = image.rotate(270, expand=True)
            elif orientation_value == 8:
                image = image.rotate(90, expand=True)
    except Exception as e:
        print(f"EXIF orientation fix failed: {e}")

    return image


# === Image Preprocessing Function ===
def preprocess_image(image: Image.Image) -> Image.Image:
    img = fix_image_orientation(image)
    # img = img.convert("L")  # Grayscale
    img.thumbnail((1024, 1024))
    img = img.resize((img.width * 2, img.height * 2), Image.BICUBIC)
    # img = ImageEnhance.Contrast(img).enhance(2.0)
    # img = ImageEnhance.Sharpness(img).enhance(2.0)
    return img

# === /parse Endpoint ===
@app.route('/parse', methods=['POST'])
@limiter.limit("10 per minute")
def parse_receipt():
    print("Received request to parse receipt")
    if 'image' not in request.files:
        return jsonify({"error": "No image file provided."}), 400
    
    file = request.files['image']
    try:
        image = Image.open(file.stream)
        processed_img = preprocess_image(image)

        buffer = io.BytesIO()
        processed_img.save(buffer, format="PNG")
        ocr_text = sample_ocr_image_data(buffer.getvalue())

        prompt = f'''The following is text that has been parsed from a receipt. 
                     Please itemize each item and price. Make sure to separate any items
                     that were purchased more than once into separate items.
                     Ensure that the total matches the sum of each individual item.
                     Only return the processed information using the following JSON schema:
                     {ReceiptSummary.model_json_schema()}
                    Parsed receipt text: {ocr_text}
                '''

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        print(response.text)
        content = response.text.strip()

        # Try validating the response as JSON using Pydantic
        try:
            # Strip markdown formatting if present
            cleaned_content = re.sub(r'^```json\s*|\s*```$', '', content.strip(), flags=re.DOTALL)
            # Validate and parse the cleaned content
            json_data = ReceiptSummary.model_validate_json(cleaned_content).model_dump()
            return jsonify(json_data), 200
        except ValidationError as ve:
            return jsonify({"error": "Failed to parse receipt json data", "details": ve.errors()}), 400

    except Exception as e:
        print(e)
        return jsonify({"error": "Could not process image", "details": str(e)}), 500


# === Expense Tracking Endpoints ===

USER_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


def get_user_id():
    user_id = request.headers.get("X-User-Id", "")
    return user_id if USER_ID_PATTERN.match(user_id) else None


def missing_user_response():
    return jsonify({"error": "Missing or invalid X-User-Id header."}), 400


@app.route('/expenses/receipts', methods=['POST'])
@limiter.limit("10 per minute")
def track_receipt():
    user_id = get_user_id()
    if not user_id:
        return missing_user_response()
    try:
        receipt = ReceiptSummary.model_validate(request.get_json(silent=True) or {})
    except ValidationError as ve:
        return jsonify({"error": "Invalid receipt data", "details": ve.errors()}), 400

    try:
        result = expense_agent.analyze_receipt(client, user_id, receipt)
        return jsonify(result), 201
    except Exception as e:
        print(e)
        return jsonify({"error": "Could not analyze receipt", "details": str(e)}), 500


@app.route('/expenses/summary', methods=['GET'])
def get_expense_summary():
    user_id = get_user_id()
    if not user_id:
        return missing_user_response()
    period = request.args.get("period", "month")
    if period not in ("day", "week", "month"):
        return jsonify({"error": 'period must be "day", "week" or "month"'}), 400
    start, end = expense_agent.period_window(period, expense_agent.date_cls.today())
    by_category = storage.spend_by_category(user_id, start, end)
    total = round(sum(row["spend"] for row in by_category), 2)
    return jsonify({"start": start, "end": end, "total": total, "by_category": by_category}), 200


@app.route('/expenses/rules', methods=['GET'])
def get_rules():
    user_id = get_user_id()
    if not user_id:
        return missing_user_response()
    return jsonify(storage.list_rules(user_id)), 200


@app.route('/expenses/rules', methods=['POST'])
def create_rule():
    user_id = get_user_id()
    if not user_id:
        return missing_user_response()
    try:
        rule = SpendingRule.model_validate(request.get_json(silent=True) or {})
    except ValidationError as ve:
        return jsonify({"error": "Invalid rule", "details": ve.errors()}), 400
    rule_id = storage.add_rule(user_id, rule.category, rule.period, rule.limit)
    return jsonify({"id": rule_id, **rule.model_dump()}), 201


@app.route('/expenses/rules/<int:rule_id>', methods=['DELETE'])
def remove_rule(rule_id):
    user_id = get_user_id()
    if not user_id:
        return missing_user_response()
    if not storage.delete_rule(user_id, rule_id):
        return jsonify({"error": "Rule not found"}), 404
    return "", 204


@app.route('/expenses/flags', methods=['GET'])
def get_flags():
    user_id = get_user_id()
    if not user_id:
        return missing_user_response()
    include_acknowledged = request.args.get("include_acknowledged", "false").lower() == "true"
    return jsonify(storage.list_flags(user_id, include_acknowledged)), 200


@app.route('/expenses/flags/<int:flag_id>/acknowledge', methods=['POST'])
def acknowledge_flag(flag_id):
    user_id = get_user_id()
    if not user_id:
        return missing_user_response()
    if not storage.acknowledge_flag(user_id, flag_id):
        return jsonify({"error": "Flag not found"}), 404
    return jsonify({"id": flag_id, "acknowledged": True}), 200


@app.route('/mock', methods=['POST'])
@limiter.limit("30 per minute")
def get_mock_data():
    with open('mock_data.json', 'r') as f:
        mock_data = json.load(f)
    return jsonify(mock_data), 200

@app.route('/health', methods=['GET'])
@limiter.limit("2 per minute")
def get_health():
    return jsonify({"status": "healthy"}), 200


# === Run Server ===
if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=8080)

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from PIL import Image
from pydantic import BaseModel, Field, ValidationError
from typing import Literal, Optional, List
import google.generativeai as genai
import re
import json
import os
from werkzeug.middleware.proxy_fix import ProxyFix
import io
from ocr_module import sample_ocr_image_data

# === Pydantic Models ===

class Item(BaseModel):
    name: str
    price: Optional[float]

class ReceiptSummary(BaseModel):
    store_name: str
    store_address: str
    store_number: Optional[int]
    items: List[Item]
    tax: Optional[float]
    total: Optional[float]
    date: Optional[str] = Field(
        pattern=r'\d{4}-\d{2}-\d{2}',
        description="Date in the format YYYY-MM-DD"
    )
    payment_method: Literal["cash", "credit", "debit", "check", "other"]

# === Configure Gemini ===
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel("gemini-2.5-flash")

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
    methods=["GET", "POST", "OPTIONS"],
)
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
    response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, Authorization")
    response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
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

        response = model.generate_content([prompt])
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

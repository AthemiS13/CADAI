import os
import re
import json
import keyring
import threading
import traceback
from pathlib import Path

from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import webview
import google.generativeai as genai
import fitz  # PyMuPDF
from PIL import Image

from session_manager import SessionManager

# Secure storage keys
KEYRING_SERVICE = "CADAI"
KEYRING_KEY_NAME = "gemini_api_key"

app = Flask(__name__)
session_manager = SessionManager()

def get_config_path():
    return Path.home() / ".cadai_config.json"

def load_config():
    p = get_config_path()
    if p.exists():
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_config(config):
    with open(get_config_path(), 'w', encoding='utf-8') as f:
        json.dump(config, f)

def strip_markdown_code(text):
    pattern = r"```(?:python|py)?\n?(.*?)```"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()

class WebApi:
    def open_file_dialog(self):
        window = webview.windows[0]
        result = window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=('Supported Files (*.txt;*.pdf;*.png;*.jpg;*.jpeg)', 'All files (*.*)')
        )
        return result if result else []
        
    def open_script_file_dialog(self):
        window = webview.windows[0]
        result = window.create_file_dialog(
            webview.SAVE_DIALOG, 
            save_filename='fusion_script.py',
            file_types=('Python Files (*.py)', 'All files (*.*)')
        )
        return result[0] if result else None

api = WebApi()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/sessions', methods=['GET'])
def get_sessions():
    return jsonify(session_manager.get_all_sessions())

@app.route('/api/sessions/new', methods=['POST'])
def new_session():
    session = session_manager.create_session()
    return jsonify(session)

@app.route('/api/sessions/<session_id>', methods=['GET'])
def get_session(session_id):
    session = session_manager.get_session(session_id)
    if session:
        return jsonify(session)
    return jsonify({"error": "Session not found"}), 404

@app.route('/api/sessions/<session_id>', methods=['DELETE'])
def delete_session(session_id):
    if session_manager.delete_session(session_id):
        return jsonify({"success": True})
    return jsonify({"error": "Failed to delete"}), 400

@app.route('/api/sessions/<session_id>/activate', methods=['POST'])
def activate_session(session_id):
    session = session_manager.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    
    config = load_config()
    script_path = config.get("script_path", "")
    if session.get("cached_code") and script_path:
        try:
            path = Path(script_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(session["cached_code"])
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"success": True, "note": "No code or script path configured"})

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json
    session_id = data.get("session_id")
    prompt = data.get("prompt")
    files = data.get("files", [])
    
    api_key = keyring.get_password(KEYRING_SERVICE, KEYRING_KEY_NAME)
    if not api_key:
        return jsonify({"error": "API Key not set"}), 400

    session = session_manager.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    # Add user message
    session_manager.add_message(session_id, "user", prompt)
    
    def generate():
        try:
            import requests
            import base64
            from io import BytesIO

            system_instruction = """SYSTEM INSTRUCTION: You are an expert Python Developer specializing in Autodesk Fusion 360 API integrations.
CRITICAL RULES AND BEST PRACTICES FOR FUSION 360 API:
1. CODE FORMAT: Output ONLY valid Python code wrapped in the standard `def run(context):` boilerplate. Do NOT include explanations outside the code block.
2. ERROR HANDLING: Always include proper `try...except` block catching exceptions and displaying them via `ui.messageBox('Failed:\\n{}'.format(traceback.format_exc()))`.
3. DOCUMENTS & COMPONENTS: If creating multiple components, ALWAYS create a new Parametric Design Document first to avoid "Part Design documents can only contain one component" errors. Use this exact snippet:
    doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    design = app.activeProduct
    design.designType = adsk.fusion.DesignTypes.ParametricDesignType
    root = design.rootComponent
4. UNITS: Fusion 360 API internal units are ALWAYS Centimeters (cm) and Radians. If a user asks for millimeters (mm), you MUST divide by 10.
5. CUT OPERATIONS: When using `CutFeatureOperation`, ensure the sketch plane and extrusion extent actually intersect a solid body. "No target body found to cut or intersect" means your geometry math is wrong and missed the body.
6. OBJECT COLLECTIONS: Always use `adsk.core.ObjectCollection.create()` when passing multiple profiles or bodies into a feature operation.
7. ROBUSTNESS: Keep your parametric math simple. If creating complex profiles like involute gears, prefer standard extrusions and circular patterns over complex loft/sweep operations which are prone to topological failures.
"""
            user_parts = []
            
            for file_info in files:
                if isinstance(file_info, str):
                    path = Path(file_info)
                    if not path.exists(): continue
                    ext = path.suffix.lower()
                    if ext == '.txt':
                        with open(path, 'r', encoding='utf-8') as f:
                            user_parts.append(f"--- File: {path.name} ---\n{f.read()}")
                    elif ext == '.pdf':
                        with open(path, "rb") as f:
                            pdf_bytes = f.read()
                        pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
                        user_parts.append({"mimeType": "application/pdf", "data": pdf_b64})
                    elif ext in ['.png', '.jpg', '.jpeg']:
                        img = Image.open(path)
                        user_parts.append(img)
                elif isinstance(file_info, dict) and file_info.get("type") == "base64":
                    b64_data = file_info.get("data", "")
                    if "," in b64_data:
                        header, encoded = b64_data.split(",", 1)
                        mime_type = header.split(":")[1].split(";")[0]
                    else:
                        encoded = b64_data
                        mime_type = "image/png"
                    user_parts.append({"mimeType": mime_type, "data": encoded})
            
            user_parts.append(prompt)
            
            payload = {
                "system_instruction": {
                    "parts": [{"text": system_instruction}]
                },
                "contents": []
            }

            if session["messages"]:
                 for msg in session["messages"][:-1]:
                     role = "user" if msg["role"] == "user" else "model"
                     payload["contents"].append({
                         "role": role,
                         "parts": [{"text": msg["content"]}]
                     })
            
            current_turn = {"role": "user", "parts": []}
            for part in user_parts:
                if isinstance(part, str):
                    current_turn["parts"].append({"text": part})
                elif isinstance(part, dict):
                    current_turn["parts"].append({"inlineData": part})
                elif hasattr(part, 'tobytes'):
                    buffered = BytesIO()
                    if part.mode in ("RGBA", "P"):
                        part = part.convert("RGB")
                    part.save(buffered, format="JPEG")
                    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
                    current_turn["parts"].append({
                        "inlineData": {
                            "mimeType": "image/jpeg",
                            "data": img_str
                        }
                    })
            
            payload["contents"].append(current_turn)

            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent?alt=sse&key={api_key}"
            response = requests.post(url, json=payload, stream=True)
            
            full_response = ""
            if response.status_code != 200:
                yield f"\n\nAPI Error {response.status_code}: {response.text}"
                return

            for line in response.iter_lines():
                if line:
                    decoded_line = line.decode('utf-8')
                    if decoded_line.startswith('data: '):
                        data_str = decoded_line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            chunk = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                            if chunk:
                                full_response += chunk
                                yield chunk
                        except Exception:
                            pass
                
            # Save model message to session
            session_manager.add_message(session_id, "model", full_response)
            
            # Extract code and save it
            code = strip_markdown_code(full_response)
            if code and "def run(" in code:
                session_manager.update_cached_code(session_id, code)
                # Auto-activate
                config = load_config()
                script_path = config.get("script_path", "")
                if script_path:
                    path = Path(script_path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with open(path, 'w', encoding='utf-8') as f:
                        f.write(code)

        except Exception as e:
            err_msg = f"Backend Error: {str(e)}\n\n```text\n{traceback.format_exc()}\n```"
            session_manager.add_message(session_id, "model", err_msg)
            yield f"\n\n{err_msg}"
            
    return Response(stream_with_context(generate()), mimetype='text/plain')

@app.route('/api/settings', methods=['GET', 'POST'])
def handle_settings():
    if request.method == 'POST':
        data = request.json
        api_key = data.get("api_key", "").strip()
        script_path = data.get("script_path", "").strip()
        
        if api_key:
            keyring.set_password(KEYRING_SERVICE, KEYRING_KEY_NAME, api_key)
        
        config = load_config()
        config["script_path"] = script_path
        save_config(config)
        
        return jsonify({"success": True})
    else:
        config = load_config()
        api_key = keyring.get_password(KEYRING_SERVICE, KEYRING_KEY_NAME) or ""
        return jsonify({
            "api_key": api_key,
            "script_path": config.get("script_path", "")
        })

def run_server():
    app.run(host='127.0.0.1', port=5000, threaded=True)

if __name__ == '__main__':
    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    webview.create_window('CADAI - Fusion 360 AI Assistant', 'http://127.0.0.1:5000', js_api=api, width=1200, height=800, text_select=True)
    webview.start()

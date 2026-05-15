"""
MktFotosbot - AI Fashion Photo Generator
Flow:
  1. Usuario manda foto + caption (Prany / Vaina / Ambas)
  2. Bot genera previews (3 fotos + 1 video) y los manda
  3. Usuario revisa → manda correcciones o confirma ("ok", "listo", "dale")
  4. Bot pregunta el nombre del producto
  5. Usuario manda el nombre → Bot crea carpeta en Drive y sube todo
"""

import os
import json
import time
import base64
import threading
import requests
from flask import Flask, request, jsonify, redirect
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import fal_client

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN        = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID      = os.environ["TELEGRAM_CHAT_ID"]
FAL_KEY               = os.environ["FAL_KEY"]
GOOGLE_CLIENT_ID      = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET  = os.environ["GOOGLE_CLIENT_SECRET"]
DRIVE_REFRESH_TOKEN   = os.environ["GOOGLE_DRIVE_REFRESH_TOKEN"]
DRIVE_FOLDER_PRANY    = os.environ["DRIVE_FOLDER_PRANY"]
DRIVE_FOLDER_VAINA    = os.environ["DRIVE_FOLDER_VAINA"]
MODELO_PRANY_DRIVE_ID = os.environ["MODELO_PRANY_DRIVE_ID"]
MODELO_VAINA_DRIVE_FOLDER = os.environ.get("MODELO_VAINA_DRIVE_FOLDER", "")

os.environ["FAL_KEY"] = FAL_KEY

# ── TikTok OAuth config ────────────────────────────────────────────────────────
TIKTOK_APP_ID      = os.environ.get("TIKTOK_APP_ID", "")
TIKTOK_APP_SECRET  = os.environ.get("TIKTOK_APP_SECRET", "")
GITHUB_PAT         = os.environ.get("GITHUB_PAT", "")
GITHUB_REPOSITORY  = os.environ.get("GITHUB_REPOSITORY", "Johnnybencu/marketing-hub")

# ── AI Quality Pipeline ────────────────────────────────────────────────────────
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY      = os.environ.get("OPENAI_API_KEY", "")
PIXELCUT_API_KEY    = os.environ.get("PIXELCUT_API_KEY", "")
KREA_API_KEY        = os.environ.get("KREA_API_KEY", "")
ADOBE_CLIENT_ID     = os.environ.get("ADOBE_CLIENT_ID", "")
ADOBE_CLIENT_SECRET = os.environ.get("ADOBE_CLIENT_SECRET", "")
ZYNG_API_KEY        = os.environ.get("ZYNG_API_KEY", "")

# ── Prompts GPT-4o por marca ──────────────────────────────────────────────────
GPT_BASE_PROMPT = (
    "Professional fashion e-commerce photo. Use this garment as the EXACT reference "
    "and show it worn by a professional female model.\n\n"
    "CRITICAL GARMENT FIDELITY — replicate EVERY detail with zero deviation:\n"
    "- COLORS: exact same hue, saturation and value — do NOT brighten, darken or shift any color\n"
    "- FABRIC: same texture, weave, sheen and weight appearance\n"
    "- BUTTONS: same quantity, size, color and exact placement\n"
    "- ZIPPERS: same type (exposed/hidden), color, length and position\n"
    "- POCKETS: same number, shape, position and style (patch/welt/flap)\n"
    "- COLLAR: exact same shape and construction (turtleneck/lapel/crew/hood etc)\n"
    "- SLEEVES: same length, cuff style and width\n"
    "- HEM: same length and finish (straight/asymmetric/curved)\n"
    "- SEAMS & STITCHING: replicate visible seam lines and topstitching\n"
    "- PRINTS & PATTERNS: exact same graphic, stripe, check or texture\n"
    "- Do NOT invent any detail not visible in the reference\n"
    "- Do NOT add or remove ANY design element\n"
    "- Do NOT alter proportions or silhouette in any way\n\n"
    "Photo requirements:\n"
    "- Pure white seamless studio background\n"
    "- Soft professional studio lighting, no harsh shadows\n"
    "- Full body or 3/4 shot, model centered and standing naturally\n"
    "- High resolution, tack-sharp fabric detail, photorealistic quality"
)

GPT_PROMPT_PRANY = (
    GPT_BASE_PROMPT + "\n\n"
    "Brand style — PRANY (sophisticated Argentine fashion):\n"
    "- ALWAYS a FEMALE model — woman, girl, she/her — NEVER a man or male figure\n"
    "- Model: tall, slender, brunette or dark straight hair, Southern European look, 22-28 years old\n"
    "- Expression: calm, confident, neutral — high-fashion editorial\n"
    "- Pose: straight, elegant, hands relaxed at sides or one hand slightly raised\n"
    "- Aesthetic: clean minimalist editorial — think Zara or Massimo Dutti campaign\n"
    "- Lighting: bright, even, soft — no dramatic shadows\n"
    "- Background: pure white seamless studio"
)

GPT_PROMPT_VAINA = (
    GPT_BASE_PROMPT + "\n\n"
    "Brand style — VAINAFASH (trendy urban fashion):\n"
    "- ALWAYS a FEMALE model — woman, girl, she/her — NEVER a man or male figure\n"
    "- Model: young woman, 18-25 years old, light brown or blonde hair, fresh and natural look\n"
    "- Expression: natural, slightly smiling, approachable and relatable\n"
    "- Pose: relaxed, slightly asymmetric — weight on one leg, casual confidence\n"
    "- Aesthetic: fresh contemporary e-commerce — think ASOS or Zara TRF campaign\n"
    "- Lighting: bright, clean, modern studio\n"
    "- Background: pure white seamless studio"
)
BOT_PUBLIC_URL     = os.environ.get("BOT_PUBLIC_URL", "https://web-production-71a27.up.railway.app")
TIKTOK_REDIRECT    = f"{BOT_PUBLIC_URL}/tiktok-callback"

TIKTOK_SECRET_MAP = {
    "prany":     {"token": "TIKTOK_TOKEN_PRANY",  "refresh": "TIKTOK_REFRESH_TOKEN_PRANY"},
    "vainafash": {"token": "TIKTOK_TOKEN_VAINA",  "refresh": "TIKTOK_REFRESH_TOKEN_VAINA"},
    "vaina":     {"token": "TIKTOK_TOKEN_VAINA",  "refresh": "TIKTOK_REFRESH_TOKEN_VAINA"},
}

# Palabras que significan "sí, está bien"
OK_WORDS = {"ok", "listo", "dale", "perfecto", "bueno", "si", "sí", "confirmed",
            "confirmo", "genial", "bien", "excelente", "bárbaro", "barbaro", "yes"}

# ── Estado global (bot solo responde a 1 usuario) ─────────────────────────────
SESSION = {
    "phase":         "idle",  # idle | waiting_instruction | generating | previewing | waiting_name | saving
    "brands":        [],
    "garment_bytes": None,
    "garment_url":   None,
    "category":      "tops",
    "flat_lay":      False,
    "caption":       "",
    "correction":    "",
    "want_video":    False,   # True solo si el caption incluye "video"
    "results": {
        # "Prany":     {"photos_bytes": [...], "video": "...", "modelo_ia": "..."},
        # "Vainafash": {...},
    },
}
SESSION_LOCK = threading.Lock()


def set_phase(phase):
    with SESSION_LOCK:
        SESSION["phase"] = phase


def get_phase():
    with SESSION_LOCK:
        return SESSION["phase"]


# ── Telegram ───────────────────────────────────────────────────────────────────
def tg_send(text, parse_mode="HTML"):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
    except Exception as e:
        print(f"[TG] Error: {e}")


def tg_send_photo(photo_url, caption=""):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHAT_ID, "photo": photo_url, "caption": caption},
            timeout=20,
        )
    except Exception as e:
        print(f"[TG] Error foto: {e}")


def tg_send_photo_bytes(photo_bytes, caption=""):
    """Manda una foto como bytes directamente (sin URL externa)."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
            files={"photo": ("photo.jpg", photo_bytes, "image/jpeg")},
            timeout=30,
        )
    except Exception as e:
        print(f"[TG] Error foto bytes: {e}")


def tg_send_video(video_url, caption=""):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendVideo",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "video": video_url,
                "caption": caption,
                "supports_streaming": True,
            },
            timeout=30,
        )
    except Exception as e:
        print(f"[TG] Error video: {e}")


# ── Google Drive ───────────────────────────────────────────────────────────────
_drive_cache = {"token": None, "expires": 0}


def get_drive_token():
    now = time.time()
    if _drive_cache["token"] and now < _drive_cache["expires"] - 60:
        return _drive_cache["token"]
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "refresh_token": DRIVE_REFRESH_TOKEN,
            "grant_type":    "refresh_token",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _drive_cache["token"]   = data["access_token"]
    _drive_cache["expires"] = now + data.get("expires_in", 3600)
    return data["access_token"]


def drive_download(file_id, token):
    resp = requests.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def drive_first_image_in_folder(folder_id, token):
    resp = requests.get(
        "https://www.googleapis.com/drive/v3/files",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "q":        f"'{folder_id}' in parents and mimeType contains 'image/'",
            "fields":   "files(id,name)",
            "orderBy":  "name",
            "pageSize": 1,
        },
        timeout=15,
    )
    resp.raise_for_status()
    files = resp.json().get("files", [])
    return files[0]["id"] if files else None


def drive_create_folder(name, parent_id, token):
    resp = requests.post(
        "https://www.googleapis.com/drive/v3/files",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def drive_upload(filename, content, mime_type, folder_id, token):
    metadata = json.dumps({"name": filename, "parents": [folder_id]})
    resp = requests.post(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
        headers={"Authorization": f"Bearer {token}"},
        files={
            "metadata": ("metadata", metadata.encode(), "application/json; charset=UTF-8"),
            "file":     (filename, content, mime_type),
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["id"]


# ── fal.ai ─────────────────────────────────────────────────────────────────────
def fal_upload(image_bytes, content_type="image/jpeg"):
    return fal_client.upload(image_bytes, content_type)


def detect_category(text):
    t = text.lower()
    if any(w in t for w in ["vestido", "mono", "enterito", "jumpsuit", "mameluco", "overall"]):
        return "one-pieces"
    if any(w in t for w in ["pantalon", "jean", "calza", "short", "bermuda", "pollera", "falda", "leggin"]):
        return "bottoms"
    return "tops"


# ── AI Quality Pipeline ────────────────────────────────────────────────────────

def gpt_generate_photo(garment_bytes, prompt, n=1):
    """
    GPT-4o via Responses API: genera foto de moda con la prenda como referencia.
    Usa image_generation tool — más accesible que gpt-image-1.
    Corre n llamadas en paralelo. Retorna lista de bytes.
    """
    if not OPENAI_API_KEY:
        print("  [GPT] Sin API key — skip")
        return []
    try:
        from openai import OpenAI
        client    = OpenAI(api_key=OPENAI_API_KEY)
        b64_image = base64.b64encode(garment_bytes).decode()

        def _one_call(_):
            try:
                resp = client.responses.create(
                    model="gpt-4o",
                    input=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "input_image",
                                "image_url": f"data:image/jpeg;base64,{b64_image}",
                            },
                            {
                                "type": "input_text",
                                "text": prompt,
                            },
                        ],
                    }],
                    tools=[{"type": "image_generation"}],
                    tool_choice="required",
                )
                for item in resp.output:
                    item_type = getattr(item, "type", "")
                    if item_type == "image_generation_call":
                        result = getattr(item, "result", None)
                        if result:
                            return base64.b64decode(result)
                    elif item_type == "message":
                        # GPT respondió con texto en lugar de imagen
                        content = getattr(item, "content", [])
                        for c in (content or []):
                            text_val = getattr(c, "text", "")
                            if text_val:
                                print(f"  [GPT] Respuesta texto (no imagen): {text_val[:120]}")
                    else:
                        print(f"  [GPT] Output item tipo: {item_type}")
                print("  [GPT] Sin imagen en respuesta output")
                return None
            except Exception as e:
                print(f"  [GPT call] Error: {e}")
                return None

        results = []
        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = [ex.submit(_one_call, i) for i in range(n)]
            for f in as_completed(futures):
                img = f.result()
                if img:
                    results.append(img)

        print(f"  [GPT] ✓ {len(results)}/{n} fotos generadas")
        return results
    except Exception as e:
        print(f"  [GPT] Error fatal: {e}")
        return []


def claude_evaluate_fidelity(original_bytes, generated_bytes, min_score=8):
    """
    Claude compara la prenda ORIGINAL vs la foto GENERADA por IA.
    Evalúa calidad general + fidelidad exacta (colores, detalles, proporciones).
    ok=True solo si score >= min_score Y fidelity >= min_score.
    """
    if not ANTHROPIC_API_KEY:
        return {"score": 7, "ok": False, "fidelity": 7, "issues": [], "feedback": "sin eval"}
    try:
        import anthropic as _anth
        import json as _json
        import re as _re

        client   = _anth.Anthropic(api_key=ANTHROPIC_API_KEY)
        orig_b64 = base64.b64encode(original_bytes).decode()
        gen_b64  = base64.b64encode(generated_bytes).decode()

        resp = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "IMAGEN 1 — Prenda original de referencia:"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": orig_b64}},
                    {"type": "text", "text": "IMAGEN 2 — Foto generada por IA (modelo vistiendo la prenda):"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": gen_b64}},
                    {"type": "text", "text": (
                        "Sos un experto en control de calidad para e-commerce de moda. "
                        "Comparar MINUCIOSAMENTE la prenda original vs la foto generada.\n\n"
                        "CHECKLIST OBLIGATORIO — verificar cada punto:\n"
                        "1. COLOR: ¿Exactamente el mismo tono, saturación y valor? "
                        "   Penalizar si se ve más brillante, más oscuro o con tinte diferente.\n"
                        "2. TEXTURA/TELA: ¿Mismo tejido, brillo y peso visual?\n"
                        "3. BOTONES: ¿Misma cantidad, tamaño, color y posición exacta?\n"
                        "4. CIERRES/ZIPPERS: ¿Mismo tipo (oculto/expuesto), color, largo y ubicación?\n"
                        "5. BOLSILLOS: ¿Misma cantidad, forma y tipo (parche/ojal/solapa)?\n"
                        "6. CUELLO: ¿Mismo tipo exacto (tortuga/solapa/redondo/capucha/etc)?\n"
                        "7. MANGAS: ¿Mismo largo y puño?\n"
                        "8. LARGO DE PRENDA: ¿Mismo largo (crop/cadera/rodilla/etc)?\n"
                        "9. COSTURAS/TOPSTITCHING: ¿Se replican las costuras visibles?\n"
                        "10. ESTAMPADO/PATRON: ¿Mismo gráfico, raya, cuadro o textura?\n"
                        "11. ELEMENTOS INVENTADOS: ¿Agrega algún detalle que NO existe en el original?\n"
                        "12. CALIDAD FOTO: ¿Fondo blanco limpio, iluminación profesional, modelo real?\n\n"
                        "SCORING:\n"
                        "- fidelity: promedio de los puntos 1-11 (qué tan idéntica es la prenda)\n"
                        "- quality: punto 12 (calidad técnica de la foto)\n"
                        "- score: promedio general\n\n"
                        "Responder SOLO con JSON válido:\n"
                        '{"score": X, "quality": X, "fidelity": X, "ok": true/false, '
                        '"issues": ["descripción MUY específica del problema 1", '
                        '"descripción MUY específica del problema 2"], '
                        '"feedback": "instrucción de 1 línea para corregir en el próximo prompt"}\n\n'
                        f"ok=true SOLO si score>={min_score} Y fidelity>={min_score}. "
                        "Issues: máximo 4, MUY específicos "
                        "(ej: 'botón superior falta', 'color viró a beige en vez de blanco roto', "
                        "'bolsillo derecho del pecho inventado no existe en original'). "
                        "Si la prenda está bien replicada pero la foto tiene un problema técnico menor, "
                        "igual indicarlo en issues."
                    )},
                ],
            }],
        )

        text = resp.content[0].text.strip()
        m = _re.search(r"\{.*\}", text, _re.DOTALL)
        if m:
            return _json.loads(m.group())
        return {"score": 5, "ok": False, "fidelity": 5, "issues": ["parse error"], "feedback": text[:80]}
    except Exception as e:
        print(f"  [Eval fidelity] Error: {e}")
        return {"score": 7, "ok": True, "fidelity": 7, "issues": [], "feedback": str(e)[:60]}


def gemini_enhance_garment(garment_bytes):
    """
    Gemini 2.0 Flash: transforma una foto de campo (piso/depósito) en una foto
    de producto limpia con fondo blanco, iluminación profesional.
    Retorna bytes de la imagen mejorada, o None si falla / no hay API key.
    """
    if not GEMINI_API_KEY:
        print("  [Gemini] Sin API key — skip")
        return None
    try:
        from google import genai as google_genai
        from google.genai import types as google_types

        client = google_genai.Client(
            api_key=GEMINI_API_KEY,
            http_options={"api_version": "v1alpha"},
        )
        response = client.models.generate_content(
            model="gemini-2.0-flash-exp-image-generation",
            contents=[
                google_types.Part.from_bytes(data=garment_bytes, mime_type="image/jpeg"),
                (
                    "This is a raw garment photo taken in a warehouse or on the floor. "
                    "Generate a professional e-commerce product photo of the EXACT SAME garment: "
                    "pure white background, professional studio lighting, garment flat-lay or on invisible mannequin, "
                    "sharp fabric details, exact same colors and design as the original. "
                    "No floor, no warehouse, no wrinkles, no shadows. Photorealistic quality."
                ),
            ],
            config=google_types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"]
            ),
        )
        for part in response.candidates[0].content.parts:
            if hasattr(part, "inline_data") and part.inline_data is not None:
                print("  [Gemini] ✓ Prenda mejorada")
                return part.inline_data.data  # bytes
        print("  [Gemini] Sin imagen en respuesta")
        return None
    except Exception as e:
        print(f"  [Gemini] Error: {e}")
        return None


def claude_evaluate_photo(image_url=None, image_bytes=None, min_score=8):
    """
    Claude evalúa la calidad de la foto para e-commerce fashion.
    Retorna dict: {"score": int 1-10, "ok": bool, "issues": list, "feedback": str}
    ok=True si score >= min_score (default 8 = excelente).
    Si no hay API key, devuelve aprobación por defecto (para no bloquear el flujo).
    """
    if not ANTHROPIC_API_KEY:
        return {"score": 7, "ok": True, "issues": [], "feedback": "sin evaluación"}

    try:
        import anthropic as anthropic_sdk
        import json as _json
        import re as _re

        client = anthropic_sdk.Anthropic(api_key=ANTHROPIC_API_KEY)

        if image_url:
            img_content = {
                "type": "image",
                "source": {"type": "url", "url": image_url},
            }
        elif image_bytes:
            img_content = {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(image_bytes).decode(),
                },
            }
        else:
            return {"score": 5, "ok": False, "issues": ["no image provided"], "feedback": ""}

        resp = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": [
                    img_content,
                    {
                        "type": "text",
                        "type": "text",
                        "text": (
                            "Evaluate this fashion e-commerce photo. Score 1-10 considering: "
                            "garment clearly visible with exact colors/design, realistic human body, "
                            "no distortions or artifacts, professional studio quality, sharp fabric details. "
                            f'Reply ONLY with JSON: {{"score": X, "ok": true/false, "issues": ["..."], "feedback": "one short line"}} '
                            f"ok=true if score>={min_score} and photo is ready for professional e-commerce."
                        ),
                    },
                ],
            }],
        )

        text = resp.content[0].text.strip()
        m = _re.search(r"\{.*\}", text, _re.DOTALL)
        if m:
            return _json.loads(m.group())
        return {"score": 5, "ok": False, "issues": ["parse error"], "feedback": text[:80]}

    except Exception as e:
        print(f"  [Eval] Error Claude: {e}")
        return {"score": 7, "ok": True, "issues": [], "feedback": f"eval error: {e}"}


def pixelcut_enhance(garment_bytes):
    """
    Pixelcut API: elimina fondo de foto de prenda.
    Docs: https://pixa.com/docs/llms.txt
    Endpoint: https://api.developer.pixelcut.ai/v1/remove-background
    Retorna bytes PNG con fondo removido, o None si falla.
    """
    if not PIXELCUT_API_KEY:
        return None
    try:
        # Subir prenda a fal.ai para obtener URL pública
        garment_url = fal_upload(garment_bytes)

        # Llamar a Pixelcut con la URL — devuelve imagen directamente (Accept: image/*)
        resp = requests.post(
            "https://api.developer.pixelcut.ai/v1/remove-background",
            headers={
                "X-API-Key": PIXELCUT_API_KEY,
                "Content-Type": "application/json",
                "Accept": "image/*",
            },
            json={"image_url": garment_url},
            timeout=30,
        )
        resp.raise_for_status()
        print("  [Pixelcut] ✓ Fondo removido")
        return resp.content  # bytes PNG
    except Exception as e:
        print(f"  [Pixelcut] Error: {e}")
        return None


def krea_enhance(garment_bytes):
    """
    Krea.ai: upscale + mejora de imagen.
    TODO: configurar KREA_API_KEY en Railway cuando tengas acceso a la API.
    https://www.krea.ai/api
    """
    if not KREA_API_KEY:
        return None
    print("  [Krea] TODO: implementar cuando API key disponible")
    return None


def zyng_product_photo(garment_bytes):
    """
    Zyng.ai: foto de producto AI para e-commerce.
    TODO: configurar ZYNG_API_KEY en Railway.
    https://zyng.ai
    """
    if not ZYNG_API_KEY:
        return None
    print("  [Zyng] TODO: implementar cuando API key disponible")
    return None


def adobe_firefly_enhance(garment_bytes):
    """
    Adobe Firefly API: generative fill / image enhancement.
    TODO: configurar ADOBE_CLIENT_ID y ADOBE_CLIENT_SECRET en Railway.
    https://developer.adobe.com/firefly-api/
    """
    if not ADOBE_CLIENT_ID:
        return None
    print("  [Firefly] TODO: implementar cuando credenciales disponibles")
    return None


# Cascade ordenado para pre-procesar la prenda antes del try-on
ENHANCE_CASCADE = [
    ("Gemini",   gemini_enhance_garment),
    ("Pixelcut", pixelcut_enhance),
    ("Krea",     krea_enhance),
    ("Zyng",     zyng_product_photo),
    ("Firefly",  adobe_firefly_enhance),
]


def enhance_garment_with_fallback(garment_bytes):
    """
    Intenta mejorar la foto de la prenda usando el cascade de servicios.
    Cada resultado es evaluado por Claude (score >= 7 para aceptar).
    Retorna (enhanced_bytes, service_name) o (None, None) si todo falla.
    """
    for service_name, fn in ENHANCE_CASCADE:
        try:
            result_bytes = fn(garment_bytes)
            if not result_bytes:
                continue
            eval_r = claude_evaluate_photo(image_bytes=result_bytes)
            score  = eval_r.get("score", 0)
            print(f"  [{service_name}] Eval: {score}/10 — {eval_r.get('feedback', '')}")
            if eval_r.get("ok"):
                return result_bytes, service_name
            else:
                issues = ", ".join(eval_r.get("issues", []))
                print(f"  [{service_name}] Rechazada ({score}/10): {issues}")
        except Exception as e:
            print(f"  [{service_name}] Error en cascade: {e}")
            continue

    print("  [Cascade] Ningún servicio mejoró la prenda — usando original")
    return None, None


def gemini_tryon(garment_bytes, n=3, feedback_notes=None, brand="Prany"):
    """
    Gemini 2.0 Flash: genera n fotos de un modelo vistiendo la prenda.
    Corre n llamadas en paralelo. Si hay feedback_notes, ajusta el prompt.
    Retorna lista de bytes (imágenes).
    """
    if not GEMINI_API_KEY:
        return []
    try:
        from google import genai as google_genai
        from google.genai import types as google_types

        client = google_genai.Client(
            api_key=GEMINI_API_KEY,
            http_options={"api_version": "v1alpha"},
        )

        feedback_str = ""
        if feedback_notes:
            feedback_str = (
                " CRITICAL — fix these issues from the previous attempt: "
                + "; ".join(feedback_notes[:4]) + "."
            )

        brand_style = (
            "FEMALE model only (woman, never a man). Model: young woman 18-25yo, light brown or blonde hair, natural look. "
            "Pose: relaxed asymmetric, casual confidence. Aesthetic: ASOS/Zara TRF."
            if brand.lower() in ("vainafash", "vaina")
            else
            "FEMALE model only (woman, never a man). Model: tall elegant brunette woman, 22-28yo, Southern European look. "
            "Pose: straight, confident. Aesthetic: Zara/Massimo Dutti editorial."
        )

        prompt = (
            "Generate a professional fashion e-commerce photo of a model wearing this EXACT garment. "
            "CRITICAL GARMENT FIDELITY: replicate every detail with zero deviation — "
            "exact same colors (no brightening/darkening), same texture, same buttons (quantity/position), "
            "same zippers, same pockets, same collar type, same sleeve length, same hem length, "
            "same seams and stitching, same print or pattern. Do NOT invent any element. "
            "Photo requirements: pure white seamless background, professional soft studio lighting, "
            "full body or 3/4 shot, realistic human proportions, no body distortions, photorealistic. "
            f"{brand_style}"
            + feedback_str
        )

        def _one_call(_):
            try:
                # Probar modelos en orden hasta que uno funcione
                gemini_models = [
                    "gemini-2.0-flash-exp-image-generation",
                    "gemini-2.0-flash-preview-image-generation",
                ]
                resp = None
                for model_name in gemini_models:
                    try:
                        resp = client.models.generate_content(
                            model=model_name,
                            contents=[
                                google_types.Part.from_bytes(data=garment_bytes, mime_type="image/jpeg"),
                                prompt,
                            ],
                            config=google_types.GenerateContentConfig(
                                response_modalities=["IMAGE", "TEXT"]
                            ),
                        )
                        print(f"  [Gemini] Usando modelo: {model_name}")
                        break
                    except Exception as me:
                        print(f"  [Gemini] {model_name} falló: {me}")
                        continue
                if resp is None:
                    print("  [Gemini] Ningún modelo disponible")
                    return None
                if not resp.candidates:
                    print("  [Gemini] Sin candidatos — posible bloqueo de contenido")
                    return None
                for part in resp.candidates[0].content.parts:
                    if hasattr(part, "inline_data") and part.inline_data is not None:
                        return part.inline_data.data
                    if hasattr(part, "text") and part.text:
                        print(f"  [Gemini] Texto en respuesta: {part.text[:120]}")
                print("  [Gemini] Sin imagen en las partes de respuesta")
                return None
            except Exception as e:
                print(f"  [Gemini call] Error: {e}")
                return None

        results = []
        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = [ex.submit(_one_call, i) for i in range(n)]
            for f in as_completed(futures):
                img = f.result()
                if img:
                    results.append(img)

        print(f"  [Gemini] ✓ {len(results)}/{n} fotos generadas")
        return results
    except Exception as e:
        print(f"  [Gemini tryon] Error: {e}")
        return []


def generate_gpt_until_approved(garment_bytes, correction="", n=3, max_attempts=4, min_score=9, brand="Prany"):
    """
    GPT-4o: genera fotos hasta que Claude apruebe calidad + fidelidad >= min_score.
    Incorpora feedback de Claude y correcciones del usuario en cada reintento.
    Retorna lista de bytes aprobados, o [] si agota intentos (para activar fallback).
    """
    feedback_history = []
    best = {"score": 0, "bytes_list": []}

    # Prompt base según marca
    base_prompt = GPT_PROMPT_VAINA if brand.lower() in ("vainafash", "vaina") else GPT_PROMPT_PRANY

    for attempt in range(1, max_attempts + 1):
        # Construir prompt: base de marca + corrección del usuario + feedback acumulado de Claude
        prompt = base_prompt
        if correction:
            prompt += f"\n\nINSTRUCCIÓN DEL USUARIO: {correction}"
        if feedback_history:
            prompt += "\n\nCORREGIR ESTOS PROBLEMAS DEL INTENTO ANTERIOR:\n- " + "\n- ".join(feedback_history[-4:])

        tg_send(f"🤖 GPT-4o — intento {attempt}/{max_attempts} ({n} fotos en paralelo)...")
        bytes_list = gpt_generate_photo(garment_bytes, prompt, n=n)

        if not bytes_list:
            tg_send(f"⚠️ Intento {attempt}: GPT no generó imágenes")
            continue

        tg_send(f"🔍 Claude comparando prenda original vs generada (mínimo {min_score}/10)...")
        approved, attempt_feedbacks = [], []

        for gen_bytes in bytes_list:
            ev = claude_evaluate_fidelity(garment_bytes, gen_bytes, min_score=min_score)
            score    = ev.get("score", 0)
            fidelity = ev.get("fidelity", 0)
            print(f"    → calidad:{score}/10  fidelidad:{fidelity}/10 — {ev.get('feedback','')}")

            if score > best["score"]:
                best["score"] = score
                best["bytes_list"] = [gen_bytes]
            elif score == best["score"] and score > 0:
                best["bytes_list"].append(gen_bytes)

            if ev.get("ok"):
                approved.append(gen_bytes)
            else:
                issues  = ev.get("issues", [])
                fb      = ev.get("feedback", "")
                attempt_feedbacks.extend(issues[:3])
                if fb and fb not in attempt_feedbacks:
                    attempt_feedbacks.append(fb)

        if approved:
            tg_send(f"✅ GPT: {len(approved)}/{len(bytes_list)} fotos aprobadas (intento {attempt})")
            return approved

        feedback_history.extend(attempt_feedbacks)
        if attempt < max_attempts:
            issues_str = "; ".join(attempt_feedbacks[:2])
            tg_send(
                f"🔄 GPT intento {attempt}: mejor {best['score']}/10 — corrigiendo...\n"
                f"<i>{issues_str[:140]}</i>"
            )

    # Agotó intentos → fallback a Gemini (no enviamos el mejor aún)
    tg_send(
        f"⚠️ GPT no alcanzó {min_score}/10 en {max_attempts} intentos "
        f"(mejor: {best['score']}/10) → probando con Gemini..."
    )
    return []


def generate_gemini_until_approved(garment_bytes, correction="", n=3, max_attempts=4, min_score=9, brand="Prany"):
    """
    Gemini: fallback si GPT falla. Mismo sistema de evaluación de fidelidad.
    Devuelve bytes aprobados, o el mejor disponible si agota intentos.
    """
    feedback_history = []
    best = {"score": 0, "bytes_list": []}

    for attempt in range(1, max_attempts + 1):
        notes = []
        if correction:
            notes.append(f"User instruction: {correction}")
        notes.extend(feedback_history[-3:])

        tg_send(f"🔮 Gemini — intento {attempt}/{max_attempts} ({n} fotos en paralelo)...")
        bytes_list = gemini_tryon(garment_bytes, n=n, feedback_notes=notes if notes else None, brand=brand)

        if not bytes_list:
            tg_send(f"⚠️ Intento {attempt}: Gemini no generó imágenes, reintentando...")
            continue

        tg_send(f"🔍 Claude comparando prenda original vs generada (mínimo {min_score}/10)...")
        approved, attempt_feedbacks = [], []

        for img_bytes in bytes_list:
            ev = claude_evaluate_fidelity(garment_bytes, img_bytes, min_score=min_score)
            score    = ev.get("score", 0)
            fidelity = ev.get("fidelity", 0)
            print(f"    → calidad:{score}/10  fidelidad:{fidelity}/10 — {ev.get('feedback','')}")

            if score > best["score"]:
                best["score"] = score
                best["bytes_list"] = [img_bytes]
            elif score == best["score"] and score > 0:
                best["bytes_list"].append(img_bytes)

            if ev.get("ok"):
                approved.append(img_bytes)
            else:
                issues = ev.get("issues", [])
                fb     = ev.get("feedback", "")
                attempt_feedbacks.extend(issues[:3])
                if fb and fb not in attempt_feedbacks:
                    attempt_feedbacks.append(fb)

        if approved:
            tg_send(f"✅ Gemini: {len(approved)}/{len(bytes_list)} fotos aprobadas (intento {attempt})")
            return approved

        feedback_history.extend(attempt_feedbacks)
        if attempt < max_attempts:
            issues_str = "; ".join(attempt_feedbacks[:2])
            tg_send(
                f"🔄 Gemini intento {attempt}: mejor {best['score']}/10 — ajustando...\n"
                f"<i>{issues_str[:120]}</i>"
            )

    # Gemini también agotó — devolver el mejor disponible
    if best["bytes_list"]:
        tg_send(
            f"⚠️ Ningún modelo alcanzó {min_score}/10.\n"
            f"Mejor resultado: <b>{best['score']}/10</b> — revisá y escribí qué cambiar."
        )
        return best["bytes_list"][:2]

    return []


def _pick_best_for_video(photos_bytes, garment_bytes):
    """
    Claude elige cuál de las fotos generadas es más apta para animar con Kling:
    prefiere figura completa visible, pose natural, modelo centrada, sin cortes.
    Si solo hay 1 foto o Claude falla, devuelve la primera.
    """
    if len(photos_bytes) == 1 or not ANTHROPIC_API_KEY:
        return photos_bytes[0]
    try:
        import anthropic as _anth
        import re as _re

        client = _anth.Anthropic(api_key=ANTHROPIC_API_KEY)

        content = [{"type": "text", "text": (
            "Tenés estas fotos de moda generadas por IA. "
            "Elegí cuál es MÁS APTA para generar un video de animación con IA (Kling). "
            "Criterios en orden de importancia:\n"
            "1. Figura femenina COMPLETA visible (cabeza + cuerpo + pies)\n"
            "2. Pose natural y estable (no demasiado rígida ni demasiado dinámica)\n"
            "3. Modelo centrada en el encuadre\n"
            "4. Sin cortes ni partes del cuerpo fuera de frame\n"
            "5. Fondo blanco limpio\n\n"
            f"Hay {len(photos_bytes)} fotos numeradas del 0 al {len(photos_bytes)-1}. "
            "Respondé SOLO con el número (0, 1, 2...) de la foto elegida."
        )}]

        for i, pb in enumerate(photos_bytes):
            content.append({"type": "text", "text": f"Foto {i}:"})
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg",
                "data": base64.b64encode(pb).decode()
            }})

        resp = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=10,
            messages=[{"role": "user", "content": content}],
        )
        text = resp.content[0].text.strip()
        m = _re.search(r"\d+", text)
        if m:
            idx = int(m.group())
            if 0 <= idx < len(photos_bytes):
                print(f"  [VIDEO] Claude eligió foto {idx} para animar")
                return photos_bytes[idx]
    except Exception as e:
        print(f"  [VIDEO] Error eligiendo foto: {e}")
    return photos_bytes[0]


def _extract_style_keywords(caption, correction, flat_lay, category):
    """Extrae keywords de estilo para el feedback loop."""
    keywords = []
    text = ((caption or "") + " " + (correction or "")).lower()

    if flat_lay:
        keywords.append("producto_fondo_blanco")
    else:
        keywords.append("con_modelo")

    style_map = {
        "exterior":  ["exterior", "calle", "outdoor", "urbano", "urban", "afuera"],
        "estudio":   ["estudio", "studio", "fondo blanco", "clean background"],
        "editorial": ["editorial", "fashion", "elegante", "lookbook"],
        "casual":    ["casual", "relajado", "everyday", "dia a dia"],
        "dinamico":  ["movimiento", "walking", "caminando", "dinamico"],
        "natural":   ["natural", "aire libre", "parque", "luz natural"],
        "oscuro":    ["oscuro", "dark", "noche", "night"],
        "colorido":  ["colorido", "vibrante", "colores"],
    }
    for style, words in style_map.items():
        if any(w in text for w in words):
            keywords.append(style)

    keywords.append(f"cat_{category}")
    return keywords or ["estudio"]


def log_photo_session(brand, product_name, category, flat_lay, caption, correction, drive_url, modelo_ia="kling_v15"):
    """
    Loguea la sesión en data/photo_log.json del repo marketing-hub via GitHub API.
    Se usa para el feedback loop: cruzar estética de fotos con conversiones GA4.
    """
    if not GITHUB_PAT:
        print("[PhotoLog] Sin GITHUB_PAT — skip")
        return

    entry = {
        "fecha":    datetime.now().strftime("%Y-%m-%d"),
        "hora":     datetime.now().strftime("%H:%M"),
        "marca":    brand,
        "producto": product_name[:60],
        "categoria": category,
        "flat_lay": flat_lay,
        "keywords": _extract_style_keywords(caption, correction, flat_lay, category),
        "modelo_ia": modelo_ia,
        "drive_url": drive_url,
    }

    repo    = GITHUB_REPOSITORY
    api_url = f"https://api.github.com/repos/{repo}/contents/data/photo_log.json"
    headers = {
        "Authorization": f"Bearer {GITHUB_PAT}",
        "Accept": "application/vnd.github+json",
    }

    try:
        # Leer archivo actual
        r = requests.get(api_url, headers=headers, timeout=10)
        if r.status_code == 200:
            data    = r.json()
            content = json.loads(base64.b64decode(data["content"]).decode())
            sha     = data["sha"]
        else:
            content = []
            sha     = None

        content.append(entry)

        # Mantener solo los últimos 90 días para no inflar el archivo
        cutoff  = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        content = [e for e in content if e.get("fecha", "") >= cutoff]

        body = {
            "message": f"📸 Photo log {brand} — {product_name[:30]}",
            "content": base64.b64encode(
                json.dumps(content, indent=2, ensure_ascii=False).encode()
            ).decode(),
        }
        if sha:
            body["sha"] = sha

        requests.put(api_url, headers=headers, json=body, timeout=15)
        print(f"[PhotoLog] ✓ {brand} — {product_name}")
    except Exception as e:
        print(f"[PhotoLog] Error: {e}")


def kling_tryon_single(garment_url, model_url, category="tops"):
    """Una sola foto con Kling Kolors v1.5 — mejor para texturas (peluche, cuero, lana)."""
    cat_desc = {
        "tops":      "upper body garment",
        "bottoms":   "lower body garment, pants or skirt",
        "one-pieces": "full body outfit, dress or jumpsuit",
    }
    result = fal_client.run(
        "fal-ai/kling/v1-5/kolors-virtual-try-on",
        arguments={
            "human_image_url":    model_url,
            "garment_image_url":  garment_url,
            "garment_description": cat_desc.get(category, "upper body garment"),
        },
    )
    return (result.get("image") or {}).get("url", "")


def fashn_tryon(garment_url, model_url, category="tops", flat_lay=False):
    """Virtual try-on con FASHN v1.6 — fallback. Devuelve lista de URLs."""
    result = fal_client.run(
        "fal-ai/fashn/tryon/v1.6",
        arguments={
            "garment_image":      garment_url,
            "model_image":        model_url,
            "category":           category,
            "flat_lay":           flat_lay,
            "num_samples":        3,
            "long_top":           False,
            "restore_background": True,
            "restore_clothes":    True,
        },
    )
    images = result.get("images", [])
    if not images:
        single = result.get("image", {})
        if single.get("url"):
            images = [single]
    return [img["url"] for img in images if img.get("url")]


def tryon_multi(garment_url, model_url, category="tops", flat_lay=False, n=3):
    """
    Genera n fotos usando Kling v1.5 como principal (mejor para texturas como peluche/cuero).
    Si Kling falla, cae automáticamente a FASHN v1.6.
    """
    photos = []

    # ── Kling primero (mejor calidad para texturas) ────────────────────────────
    print("  [Kling] Intentando try-on...")
    try:
        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = [ex.submit(kling_tryon_single, garment_url, model_url, category)
                       for _ in range(n)]
            for f in as_completed(futures):
                url = f.result()
                if url:
                    photos.append(url)
        if photos:
            print(f"  [Kling] ✓ {len(photos)} fotos generadas")
            return photos
        print("  [Kling] Sin resultados — fallback a FASHN")
    except Exception as e:
        print(f"  [Kling] Error: {e} — fallback a FASHN")

    # ── FASHN como fallback ────────────────────────────────────────────────────
    print("  [FASHN] Intentando try-on (fallback)...")
    try:
        photos = fashn_tryon(garment_url, model_url, category, flat_lay=flat_lay)
        print(f"  [FASHN] ✓ {len(photos)} fotos generadas")
    except Exception as e:
        print(f"  [FASHN] Error: {e}")

    return photos


def kling_video(image_url, prompt):
    """Genera video con Kling v3 Pro. Devuelve URL."""
    result = fal_client.run(
        "fal-ai/kling-video/v3/pro/image-to-video",
        arguments={
            "image_url":    image_url,
            "prompt":       prompt,
            "duration":     "5",
            "aspect_ratio": "9:16",
        },
    )
    return result.get("video", {}).get("url", "")


# ── Pipeline de generación ─────────────────────────────────────────────────────
def generate_for_brand(brand, garment_bytes, garment_url, category, correction="", flat_lay=False, want_video=False):
    """
    Genera fotos y video para una marca.
    Si hay corrección del usuario, se incorpora al prompt del video.
    Devuelve dict con {"photos": [...], "video": "...", "model_url": "..."}.
    """
    drive_token = get_drive_token()

    # Modelo de referencia + video prompt por marca
    if brand == "Prany":
        model_bytes  = drive_download(MODELO_PRANY_DRIVE_ID, drive_token)
        video_prompt = (
            "Elegant female fashion model walking slowly and gracefully, "
            "wearing the outfit, smooth fluid movement, soft studio lighting, "
            "pure white background, high-end fashion editorial style, "
            "full body visible, photorealistic, cinematic quality"
        )
    else:  # Vainafash
        model_file_id = drive_first_image_in_folder(MODELO_VAINA_DRIVE_FOLDER, drive_token) if MODELO_VAINA_DRIVE_FOLDER else None
        if model_file_id:
            model_bytes = drive_download(model_file_id, drive_token)
        else:
            model_bytes = garment_bytes
        video_prompt = (
            "Young female fashion model walking confidently, "
            "wearing the outfit, natural relaxed movement, bright studio lighting, "
            "pure white background, fresh contemporary e-commerce style, "
            "full body visible, photorealistic, cinematic quality"
        )

    if correction:
        video_prompt = f"{video_prompt}, {correction}"

    photos_bytes = []
    modelo_ia    = "failed"

    # ── Paso 1: GPT-4o (principal) ────────────────────────────────────────
    photos_bytes = generate_gpt_until_approved(
        garment_bytes, correction=correction, n=3, max_attempts=4, min_score=9, brand=brand
    )
    if photos_bytes:
        modelo_ia = "gpt4o"

    # ── Paso 2: Gemini (fallback si GPT falla) ────────────────────────────
    if not photos_bytes:
        photos_bytes = generate_gemini_until_approved(
            garment_bytes, correction=correction, n=3, max_attempts=4, min_score=9, brand=brand
        )
        if photos_bytes:
            modelo_ia = "gemini_tryon"

    if not photos_bytes:
        tg_send(
            f"❌ <b>{brand}</b>: ningún modelo generó fotos aceptables.\n"
            f"Probá con otra foto de la prenda (más luz, fondo limpio, prenda extendida)."
        )
        return {"photos_bytes": [], "photos": [], "video": "", "modelo_ia": "failed"}

    # ── Video: solo si want_video=True ────────────────────────────────────
    video = ""
    if want_video:
        try:
            tg_send(f"🎬 <b>{brand}</b>: generando video...")

            # Claude elige la foto más apta para animar (figura completa, pose natural)
            best_photo_bytes = _pick_best_for_video(photos_bytes, garment_bytes)
            best_url = fal_upload(best_photo_bytes)

            # Hasta 2 intentos de video
            for vid_attempt in range(1, 3):
                try:
                    video = kling_video(best_url, video_prompt)
                    if video:
                        print(f"  [VIDEO] ✓ {brand} — intento {vid_attempt}")
                        break
                    print(f"  [VIDEO] Intento {vid_attempt} sin resultado, reintentando...")
                except Exception as ve:
                    print(f"  [VIDEO] Error intento {vid_attempt}: {ve}")
            if not video:
                tg_send(f"⚠️ <b>{brand}</b>: no se pudo generar el video (fotos ok ✅)")
        except Exception as e:
            print(f"[VIDEO] Error fatal {brand}: {e}")
            tg_send(f"⚠️ <b>{brand}</b>: error en video — {str(e)[:100]}")

    return {
        "photos_bytes": photos_bytes,
        "photos":       [],
        "video":        video,
        "model_url":    None,
        "modelo_ia":    modelo_ia,
    }


def send_previews(brand, result):
    """Manda las fotos y video de una marca por Telegram."""
    photos_bytes = result.get("photos_bytes", [])
    photos       = result.get("photos", [])   # URLs legacy
    video        = result.get("video", "")
    total        = len(photos_bytes) + len(photos)

    if total == 0:
        return

    tg_send(f"🎨 <b>{brand}</b> — {total} foto{'s' if total > 1 else ''}{' + 1 video' if video else ''}:")

    for i, b in enumerate(photos_bytes):
        tg_send_photo_bytes(b, f"{brand} — Foto {i+1}/{total}")
        time.sleep(0.4)

    for i, url in enumerate(photos):
        tg_send_photo(url, f"{brand} — Foto {len(photos_bytes)+i+1}/{total}")
        time.sleep(0.4)

    if video:
        tg_send_video(video, f"{brand} — Video")


def run_generation(brands, garment_bytes, category, correction="", flat_lay=False, want_video=False):
    """
    Corre el pipeline completo para todas las marcas,
    manda previews y queda en fase 'previewing'.
    """
    set_phase("generating")

    with SESSION_LOCK:
        SESSION["garment_url"] = None
        SESSION["results"] = {}

    for brand in brands:
        tg_send(f"🔄 <b>{brand}</b>: generando fotos (puede tardar 2-3 min)... ☕")
        try:
            result = generate_for_brand(brand, garment_bytes, None, category, correction, flat_lay, want_video)
            with SESSION_LOCK:
                SESSION["results"][brand] = result
            send_previews(brand, result)
        except Exception as e:
            import traceback
            print(f"[ERROR] {brand}: {traceback.format_exc()}")
            tg_send(f"❌ <b>{brand}</b>: Error — {str(e)[:200]}")

    tg_send(
        "✅ <b>Listo para revisar.</b>\n\n"
        "• Si está bien → <b>ok</b> / <b>listo</b> / <b>dale</b>\n"
        "• Si querés cambios → describí qué modificar\n"
        "  Ej: <i>\"más luminoso\"</i>, <i>\"pose diferente\"</i>, <i>\"solo Prany\"</i>"
    )
    set_phase("previewing")


def save_to_drive(product_name):
    """Crea carpetas en Drive y sube todo. Solo se llama tras confirmación."""
    set_phase("saving")

    date_str  = datetime.now().strftime("%Y%m%d")
    safe_name = product_name[:40].replace(" ", "_").replace("/", "-")
    folder_name = f"{date_str}_{safe_name}"

    drive_token = get_drive_token()
    tg_send(f"💾 Guardando <b>{product_name}</b> en Drive...")

    with SESSION_LOCK:
        results = dict(SESSION["results"])
        brands  = list(SESSION["brands"])

    for brand in brands:
        result = results.get(brand)
        if not result:
            continue

        parent_folder = DRIVE_FOLDER_PRANY if brand == "Prany" else DRIVE_FOLDER_VAINA

        try:
            subfolder_id = drive_create_folder(folder_name, parent_folder, drive_token)

            # Fotos desde bytes (Gemini) — subida directa sin descarga
            photos_bytes = result.get("photos_bytes", [])
            for i, img_bytes in enumerate(photos_bytes):
                drive_upload(f"foto_{i+1}.jpg", img_bytes, "image/jpeg", subfolder_id, drive_token)

            # Fotos desde URL (legacy) — descargar y subir
            for j, img_url in enumerate(result.get("photos", [])):
                img_bytes = requests.get(img_url, timeout=30).content
                drive_upload(f"foto_{len(photos_bytes)+j+1}.jpg", img_bytes, "image/jpeg", subfolder_id, drive_token)

            if result["video"]:
                vid_bytes = requests.get(result["video"], timeout=60).content
                drive_upload("video_1.mp4", vid_bytes, "video/mp4", subfolder_id, drive_token)

            drive_link = f"https://drive.google.com/drive/folders/{subfolder_id}"
            tg_send(
                f"✅ <b>{brand}</b> guardado\n"
                f"📁 <a href='{drive_link}'>{folder_name}</a>"
            )

            # Feedback loop: loguear sesión para cruzar con GA4 después
            with SESSION_LOCK:
                _cap      = SESSION.get("caption", "")
                _corr     = SESSION.get("correction", "")
                _fl       = SESSION.get("flat_lay", False)
                _cat      = SESSION.get("category", "tops")
                _modelo   = result.get("modelo_ia", "kling_v15")
            log_photo_session(brand, product_name, _cat, _fl, _cap, _corr, drive_link, _modelo)

        except Exception as e:
            print(f"[DRIVE] Error {brand}: {e}")
            tg_send(f"❌ Error guardando <b>{brand}</b>: {str(e)[:150]}")

    # Mantener prenda + marca en sesión para permitir correcciones sin re-subir
    with SESSION_LOCK:
        _brands = list(SESSION["brands"])
    tg_send(
        f"🎉 ¡Todo guardado!\n\n"
        f"Podés seguir enviando correcciones sobre la misma prenda "
        f"(<b>{', '.join(_brands)}</b>) sin mandar la foto de nuevo.\n"
        f"O mandá una foto nueva para empezar con otra prenda."
    )
    with SESSION_LOCK:
        SESSION["phase"]       = "idle"
        SESSION["garment_url"] = None
        SESSION["correction"]  = ""
        SESSION["results"]     = {}
        # garment_bytes, brands, category, flat_lay, want_video se MANTIENEN


# ── Handlers de mensajes ───────────────────────────────────────────────────────
def handle_photo(message):
    """Usuario mandó una foto. Parsea caption y arranca generación."""
    phase = get_phase()

    if phase == "generating" or phase == "saving":
        tg_send("⏳ Todavía estoy generando, esperá un momento...")
        return

    caption = (message.get("caption") or "").strip()
    cap_low = caption.lower()

    # Detectar marcas
    if cap_low.startswith("prany"):
        brands  = ["Prany"]
    elif cap_low.startswith("vaina"):
        brands  = ["Vainafash"]
    elif any(w in cap_low for w in ["ambas", "las dos", "ambos", "ambas"]):
        brands  = ["Prany", "Vainafash"]
    else:
        # Por default preguntamos (o usamos ambas si no hay caption)
        if not caption:
            tg_send(
                "❓ Escribí la marca en el caption:\n"
                "• <b>Prany</b>\n"
                "• <b>Vaina</b>\n"
                "• <b>Ambas</b>"
            )
            return
        # Si hay caption pero sin marca, asumir ambas
        brands = ["Prany", "Vainafash"]

    # Hint de categoría desde el caption (si lo dieron)
    category = detect_category(caption)

    # flat_lay solo si el usuario indica explícitamente que la foto es producto en fondo blanco
    flat_lay = any(w in cap_low for w in ["fondo blanco", "flat lay", "flatlay", "producto", "hanger", "percha"])

    # Video solo si el caption incluye "video" explícitamente
    want_video = "video" in cap_low

    # Descargar foto
    photo_list = message.get("photo", [])
    document   = message.get("document", {})

    if photo_list:
        file_id = max(photo_list, key=lambda p: p.get("file_size", 0))["file_id"]
    elif document and document.get("mime_type", "").startswith("image"):
        file_id = document["file_id"]
    else:
        tg_send("❌ No encontré imagen en el mensaje.")
        return

    try:
        fi = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=10,
        ).json()
        photo_bytes = requests.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{fi['result']['file_path']}",
            timeout=30,
        ).content
    except Exception as e:
        tg_send(f"❌ Error descargando foto: {e}")
        return

    # Guardar en sesión
    with SESSION_LOCK:
        SESSION["brands"]        = brands
        SESSION["garment_bytes"] = photo_bytes
        SESSION["category"]      = category
        SESSION["flat_lay"]      = flat_lay
        SESSION["caption"]       = caption
        SESSION["correction"]    = ""
        SESSION["want_video"]    = want_video

    modo = "fondo blanco ✅" if flat_lay else "foto de campo"
    video_txt = " + 🎬 video" if want_video else ""
    tg_send(
        f"📸 Foto recibida ({len(photo_bytes) // 1024} KB)\n"
        f"Marca(s): <b>{', '.join(brands)}</b> | {modo}{video_txt}\n\n"
        f"¿Tenés alguna instrucción para las fotos?\n"
        f"<i>Ej: \"que sea en negro\", \"pose dinámica\", \"fondo gris\", \"sin capucha\"</i>\n"
        f"O escribí <b>no</b> para generar ya."
    )
    with SESSION_LOCK:
        SESSION["phase"] = "waiting_instruction"


def handle_text(message):
    """Usuario mandó texto. Depende de la fase actual."""
    text  = message.get("text", "").strip()
    words = set(text.lower().split())
    phase = get_phase()

    # ── /start o saludo ───────────────────────────────────────────────────────
    if text.startswith("/start") or text.lower() in ("hola", "start"):
        tg_send(
            "👗 <b>MktFotos Bot</b>\n\n"
            "<b>Flujo:</b>\n"
            "1️⃣ Mandá foto + caption con la marca:\n"
            "   • <code>Prany</code> / <code>Vaina</code> / <code>Ambas</code>\n"
            "   • Agregá <code>video</code> si querés video (~$0.28 USD extra)\n\n"
            "2️⃣ El bot te pregunta si tenés alguna instrucción:\n"
            "   <i>\"en negro\", \"pose dinámica\", \"fondo gris\"</i>\n"
            "   O escribí <b>no</b> para generar directo\n\n"
            "3️⃣ Se generan 3 fotos — Claude evalúa fidelidad (9/10 mínimo)\n\n"
            "4️⃣ Revisás las fotos y podés:\n"
            "   • Escribir correcciones → regenera la misma prenda\n"
            "   • Decir <b>ok / listo</b> → te pide el nombre y guarda en Drive\n\n"
            "💡 Después de guardar podés seguir corrigiendo la misma prenda sin remandar la foto."
        )
        return

    # ── Cancelar en cualquier momento ─────────────────────────────────────────
    if text.lower() in ("cancelar", "cancel", "/cancel", "borrar"):
        with SESSION_LOCK:
            SESSION["phase"]        = "idle"
            SESSION["brands"]       = []
            SESSION["garment_bytes"] = None
            SESSION["results"]      = {}
        tg_send("🗑️ Sesión cancelada. Mandame una nueva foto cuando quieras.")
        return

    # ── Fase: waiting_instruction — espera instrucción antes de generar ──────────
    if phase == "waiting_instruction":
        with SESSION_LOCK:
            brands        = list(SESSION["brands"])
            garment_bytes = SESSION["garment_bytes"]
            category      = SESSION["category"]
            flat_lay      = SESSION["flat_lay"]
            want_video    = SESSION.get("want_video", False)
            # Si dice "no", generamos sin instrucción
            instruction = "" if text.lower() in ("no", "nop", "nope", "ninguna", "sin comentarios", "dale") else text
            SESSION["correction"] = instruction

        if instruction:
            tg_send(
                f"✅ Instrucción guardada: <i>\"{instruction}\"</i>\n"
                f"🔄 Generando con esa indicación desde el primer intento..."
            )
        else:
            tg_send("🔄 Generando sin instrucción adicional...")

        threading.Thread(
            target=run_generation,
            args=(brands, garment_bytes, category, instruction, flat_lay, want_video),
            daemon=True,
        ).start()
        return

    # ── Fase: previewing — espera OK o corrección ──────────────────────────────
    if phase == "previewing":
        if words & OK_WORDS:
            # Usuario confirmó → pedir nombre
            set_phase("waiting_name")
            tg_send("✏️ ¿Cómo se llama el producto? (ese nombre se va a usar para la carpeta en Drive)")
            return
        else:
            # Corrección manual: se agrega al prompt y regenera
            with SESSION_LOCK:
                brands        = list(SESSION["brands"])
                garment_bytes = SESSION["garment_bytes"]
                category      = SESSION["category"]
                flat_lay      = SESSION["flat_lay"]
                want_video    = SESSION.get("want_video", False)
                SESSION["correction"] = text

            tg_send(
                f"✏️ Corrección: <i>\"{text}\"</i>\n"
                f"🔄 Regenerando <b>{', '.join(brands)}</b> con la misma prenda..."
            )
            threading.Thread(
                target=run_generation,
                args=(brands, garment_bytes, category, text, flat_lay, want_video),
                daemon=True,
            ).start()
        return

    # ── Fase: waiting_name — espera el nombre del producto ────────────────────
    if phase == "waiting_name":
        product_name = text
        threading.Thread(
            target=save_to_drive,
            args=(product_name,),
            daemon=True,
        ).start()
        return

    # ── Fase idle u otras ─────────────────────────────────────────────────────
    if phase in ("generating", "saving"):
        tg_send("⏳ Estoy trabajando, esperá un momento...")
    else:
        # ¿Hay prenda de una sesión anterior guardada?
        with SESSION_LOCK:
            garment_bytes = SESSION.get("garment_bytes")
            brands        = list(SESSION.get("brands", []))
            category      = SESSION.get("category", "tops")
            flat_lay      = SESSION.get("flat_lay", False)
            want_video    = SESSION.get("want_video", False)

        if garment_bytes and brands:
            # Tratar el texto como corrección sobre la prenda anterior
            with SESSION_LOCK:
                SESSION["correction"] = text
            tg_send(
                f"✏️ Corrección: <i>\"{text}\"</i>\n"
                f"🔄 Regenerando <b>{', '.join(brands)}</b> con la misma prenda..."
            )
            threading.Thread(
                target=run_generation,
                args=(brands, garment_bytes, category, text, flat_lay, want_video),
                daemon=True,
            ).start()
        else:
            tg_send(
                "📸 Mandame una foto de la prenda con el caption de la marca:\n"
                "• <b>Prany</b> / <b>Vaina</b> / <b>Ambas</b>\n\n"
                "Añadí <b>video</b> al caption si querés generar video también.\n"
                "<i>Ej: \"Prany video\" o \"Ambas video\"</i>"
            )


# ── Webhook ────────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    update  = request.get_json(force=True, silent=True) or {}
    message = update.get("message", {})

    chat_id = str(message.get("chat", {}).get("id", ""))
    if chat_id != TELEGRAM_CHAT_ID:
        return jsonify({"ok": True})

    if "photo" in message or (
        "document" in message and
        message["document"].get("mime_type", "").startswith("image")
    ):
        threading.Thread(target=handle_photo, args=(message,), daemon=True).start()
    elif "text" in message:
        handle_text(message)

    return jsonify({"ok": True})


@app.route("/health")
def health():
    with SESSION_LOCK:
        phase = SESSION["phase"]
    return jsonify({"status": "ok", "phase": phase})


@app.route("/")
def index():
    return jsonify({"bot": "MktFotosbot", "status": "running"})


# ── TikTok OAuth ───────────────────────────────────────────────────────────────

def _gh_public_key():
    """Obtiene la public key del repo para encriptar GitHub Secrets."""
    if not GITHUB_PAT:
        return None, None
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/public-key",
            headers={"Authorization": f"Bearer {GITHUB_PAT}",
                     "Accept": "application/vnd.github+json"},
            timeout=10,
        )
        if r.status_code == 200:
            d = r.json()
            return d["key"], d["key_id"]
    except Exception as e:
        print(f"[GITHUB] public_key error: {e}")
    return None, None


def _gh_update_secret(name, value, pub_key, key_id):
    """Encripta y sube un secret a GitHub Actions via API."""
    try:
        from nacl import public as nacl_public
        pk = nacl_public.PublicKey(base64.b64decode(pub_key))
        encrypted = base64.b64encode(
            nacl_public.SealedBox(pk).encrypt(value.encode())
        ).decode()
    except ImportError:
        print("[GITHUB] PyNaCl no instalado")
        return False
    try:
        r = requests.put(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/secrets/{name}",
            headers={"Authorization": f"Bearer {GITHUB_PAT}",
                     "Accept": "application/vnd.github+json"},
            json={"encrypted_value": encrypted, "key_id": key_id},
            timeout=10,
        )
        return r.status_code in (201, 204)
    except Exception as e:
        print(f"[GITHUB] update_secret error: {e}")
        return False


@app.route("/tiktok-auth/<marca>")
def tiktok_auth(marca):
    """
    Redirige al flujo OAuth de TikTok.
    Uso: abrir en el navegador → autorizar → callback automático.
    Ejemplo: https://web-production-71a27.up.railway.app/tiktok-auth/prany
    """
    marca_lower = marca.lower()
    if marca_lower not in TIKTOK_SECRET_MAP:
        return f"Marca '{marca}' no reconocida. Usar: prany, vainafash", 400
    if not TIKTOK_APP_ID:
        return "TIKTOK_APP_ID no configurado en Railway", 500

    auth_url = (
        f"https://business-api.tiktok.com/portal/auth"
        f"?app_id={TIKTOK_APP_ID}"
        f"&state={marca_lower}"
        f"&redirect_uri={TIKTOK_REDIRECT}"
    )
    print(f"[TIKTOK AUTH] Redirigiendo {marca_lower} → {auth_url}")
    return redirect(auth_url)


@app.route("/tiktok-callback")
def tiktok_callback():
    """
    Recibe el auth_code de TikTok después del OAuth.
    Intercambia por access_token + refresh_token y los guarda en GitHub Secrets.
    """
    auth_code = request.args.get("auth_code", "").strip()
    state     = request.args.get("state", "").lower().strip()

    print(f"[TIKTOK CALLBACK] state={state} auth_code={'OK' if auth_code else 'MISSING'}")

    if not auth_code:
        return "<h2>❌ Error</h2><p>No se recibió auth_code de TikTok.</p>", 400
    if state not in TIKTOK_SECRET_MAP:
        return f"<h2>❌ Error</h2><p>Estado '{state}' no reconocido.</p>", 400

    # Intercambiar código por tokens
    try:
        r = requests.post(
            "https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/",
            json={"app_id": TIKTOK_APP_ID, "secret": TIKTOK_APP_SECRET, "auth_code": auth_code},
            timeout=15,
        )
        data = r.json()
    except Exception as e:
        tg_send(f"❌ <b>TikTok OAuth {state}</b>: error de red — {e}")
        return f"<h2>❌ Error de red</h2><p>{e}</p>", 500

    if data.get("code") != 0:
        msg = f"{data.get('message', 'Error desconocido')} (code={data.get('code')})"
        tg_send(f"❌ <b>TikTok OAuth {state}</b>: {msg}")
        return f"<h2>❌ TikTok rechazó el código</h2><p>{msg}</p>", 400

    token_data    = data.get("data", {})
    access_token  = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    expires_h     = token_data.get("access_token_expires_in", 86400) // 3600
    refresh_days  = token_data.get("refresh_token_expires_in", 0) // 86400

    # Guardar en GitHub Secrets
    secrets = TIKTOK_SECRET_MAP[state]
    pub_key, key_id = _gh_public_key()
    resultados = []

    if pub_key:
        ok1 = _gh_update_secret(secrets["token"],   access_token,  pub_key, key_id)
        ok2 = _gh_update_secret(secrets["refresh"],  refresh_token, pub_key, key_id)
        resultados.append(f"{'✅' if ok1 else '❌'} {secrets['token']}")
        resultados.append(f"{'✅' if ok2 else '❌'} {secrets['refresh']}")
    else:
        resultados.append("❌ Sin GITHUB_PAT — secrets no actualizados")

    marca_display = state.capitalize()
    resumen = "\n".join(resultados)
    tg_send(
        f"🎉 <b>TikTok {marca_display} — OAuth completado</b>\n\n"
        f"{resumen}\n\n"
        f"⏱ Access token: válido {expires_h}h\n"
        f"🔄 Refresh token: válido {refresh_days}d"
    )

    return f"""
    <html><body style="font-family:sans-serif;max-width:500px;margin:60px auto;text-align:center">
    <h2>✅ TikTok {marca_display} autorizado</h2>
    <p>Tokens guardados en GitHub Secrets automáticamente.</p>
    <pre style="text-align:left;background:#f0f0f0;padding:16px;border-radius:8px">{resumen}</pre>
    <p>Podés cerrar esta pestaña.</p>
    </body></html>
    """, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[BOT] Puerto {port}")
    app.run(host="0.0.0.0", port=port)

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
from datetime import datetime
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
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PIXELCUT_API_KEY  = os.environ.get("PIXELCUT_API_KEY", "")
KREA_API_KEY      = os.environ.get("KREA_API_KEY", "")
ADOBE_CLIENT_ID   = os.environ.get("ADOBE_CLIENT_ID", "")
ADOBE_CLIENT_SECRET = os.environ.get("ADOBE_CLIENT_SECRET", "")
ZYNG_API_KEY      = os.environ.get("ZYNG_API_KEY", "")
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
    "phase":        "idle",  # idle | generating | previewing | waiting_name | saving
    "brands":       [],
    "garment_bytes": None,
    "garment_url":  None,   # URL en fal.ai
    "category":     "tops",
    "flat_lay":     False,  # True solo si foto con fondo blanco limpio
    "caption":      "",     # caption original del usuario (para feedback loop)
    "correction":   "",     # última corrección del usuario
    "results": {
        # "Prany":     {"photos": [...], "video": "...", "model_url": "..."},
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

        client = google_genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.0-flash-preview-image-generation",
            contents=[
                google_types.Part.from_bytes(data=garment_bytes, mime_type="image/jpeg"),
                google_types.Part.from_text(
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


def claude_evaluate_photo(image_url=None, image_bytes=None):
    """
    Claude evalúa la calidad de la foto para e-commerce fashion.
    Retorna dict: {"score": int 1-10, "ok": bool, "issues": list, "feedback": str}
    ok=True si score >= 7 y la prenda es claramente visible sin defectos graves.
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
                        "text": (
                            "Evaluate this fashion e-commerce photo. Score 1-10 considering: "
                            "garment clearly visible, no body/face distortions, no artifacts or blurs, "
                            "professional look, fabric details preserved. "
                            'Reply ONLY with JSON: {"score": X, "ok": true/false, "issues": ["..."], "feedback": "one short line"} '
                            "ok=true if score>=7 and garment has no major defects."
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
            "human_image":        model_url,
            "garment_image":      garment_url,
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
def generate_for_brand(brand, garment_bytes, garment_url, category, correction="", flat_lay=False):
    """
    Genera fotos y video para una marca.
    Si hay corrección del usuario, se incorpora al prompt del video.
    Devuelve dict con {"photos": [...], "video": "...", "model_url": "..."}.
    """
    drive_token = get_drive_token()

    # Modelo de referencia
    if brand == "Prany":
        model_bytes   = drive_download(MODELO_PRANY_DRIVE_ID, drive_token)
        video_prompt  = (
            "Fashion model walking elegantly, wearing the outfit, "
            "smooth movement, soft studio lighting, clean background, fashion editorial"
        )
    else:  # Vainafash
        model_file_id = drive_first_image_in_folder(MODELO_VAINA_DRIVE_FOLDER, drive_token) if MODELO_VAINA_DRIVE_FOLDER else None
        if model_file_id:
            model_bytes = drive_download(model_file_id, drive_token)
        else:
            model_bytes = garment_bytes
        video_prompt = (
            "POV walking video, looking down at trendy outfit, "
            "urban street style, natural lighting, aesthetic movement"
        )

    # Si el usuario mandó una corrección, la agregamos al prompt del video
    if correction:
        video_prompt = f"{video_prompt}, {correction}"

    model_url = fal_upload(model_bytes)

    # ── Paso 1: Mejorar prenda con cascade (Gemini → Pixelcut → ...) ────────
    tg_send(f"🔮 <b>{brand}</b>: mejorando imagen con IA...")
    enhanced_bytes, enhance_service = enhance_garment_with_fallback(garment_bytes)

    if enhanced_bytes:
        tg_send(f"✅ <b>{brand}</b>: prenda mejorada por <b>{enhance_service}</b>")
        tryon_garment_url = fal_upload(enhanced_bytes)
        modelo_ia_usado   = f"{enhance_service.lower()}_kling"
    else:
        tryon_garment_url = garment_url
        modelo_ia_usado   = "kling_v15"

    # ── Paso 2: Try-on con prenda (mejorada o original) ───────────────────
    photos = tryon_multi(tryon_garment_url, model_url, category, flat_lay=flat_lay, n=3)

    # ── Paso 3: Evaluar resultados del try-on con Claude ─────────────────
    if photos and ANTHROPIC_API_KEY:
        tg_send(f"🔍 <b>{brand}</b>: evaluando {len(photos)} fotos con Claude...")
        good_photos, bad_photos = [], []
        for url in photos:
            ev = claude_evaluate_photo(image_url=url)
            print(f"    → {ev.get('score',0)}/10: {ev.get('feedback','')}")
            if ev.get("ok"):
                good_photos.append(url)
            else:
                bad_photos.append((url, ev))

        if good_photos:
            tg_send(f"✅ <b>{brand}</b>: {len(good_photos)}/{len(photos)} fotos aprobadas")
            photos = good_photos
        else:
            issues = "; ".join(
                ev.get("feedback", "") for _, ev in bad_photos[:2] if ev.get("feedback")
            )
            tg_send(
                f"⚠️ <b>{brand}</b>: calidad baja detectada\n"
                f"<i>{issues}</i>\n"
                f"Revisá y decime si querés regenerar o está bien."
            )

    # ── Paso 4: Video con la primera foto aprobada ────────────────────────
    video = ""
    if photos:
        try:
            video = kling_video(photos[0], video_prompt)
        except Exception as e:
            print(f"[KLING] Error video: {e}")

    return {"photos": photos, "video": video, "model_url": model_url, "modelo_ia": modelo_ia_usado}


def send_previews(brand, result):
    """Manda las fotos y video de una marca por Telegram."""
    photos = result["photos"]
    video  = result["video"]

    tg_send(f"🎨 <b>{brand}</b> — {len(photos)} fotos{' + 1 video' if video else ''}:")

    for i, url in enumerate(photos):
        tg_send_photo(url, f"{brand} - Foto {i+1}/{len(photos)}")
        time.sleep(0.4)

    if video:
        tg_send_video(video, f"{brand} - Video")


def run_generation(brands, garment_bytes, category, correction="", flat_lay=False):
    """
    Corre el pipeline completo para todas las marcas,
    manda previews y queda en fase 'previewing'.
    """
    set_phase("generating")

    # Subir prenda a fal.ai (una sola vez, compartida entre marcas)
    tg_send("⬆️ Subiendo prenda a IA...")
    garment_url = fal_upload(garment_bytes)

    with SESSION_LOCK:
        SESSION["garment_url"] = garment_url
        SESSION["results"] = {}

    for brand in brands:
        tg_send(f"🔄 <b>{brand}</b>: generando fotos (1-2 min)... ☕")
        try:
            result = generate_for_brand(brand, garment_bytes, garment_url, category, correction, flat_lay)
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

            for i, img_url in enumerate(result["photos"]):
                img_bytes = requests.get(img_url, timeout=30).content
                drive_upload(f"foto_{i+1}.jpg", img_bytes, "image/jpeg", subfolder_id, drive_token)

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

    tg_send("🎉 ¡Todo guardado! Mandame otra foto cuando quieras.")
    with SESSION_LOCK:
        SESSION["phase"]        = "idle"
        SESSION["brands"]       = []
        SESSION["garment_bytes"] = None
        SESSION["garment_url"]  = None
        SESSION["flat_lay"]     = False
        SESSION["results"]      = {}
        SESSION["correction"]   = ""


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

    modo = "fondo blanco ✅" if flat_lay else "foto de campo"
    tg_send(
        f"📸 Foto recibida ({len(photo_bytes) // 1024} KB) | "
        f"Marca(s): <b>{', '.join(brands)}</b> | Modo: {modo}\n"
        f"⏳ Generando previews..."
    )

    threading.Thread(
        target=run_generation,
        args=(brands, photo_bytes, category, "", flat_lay),
        daemon=True,
    ).start()


def handle_text(message):
    """Usuario mandó texto. Depende de la fase actual."""
    text  = message.get("text", "").strip()
    words = set(text.lower().split())
    phase = get_phase()

    # ── /start o saludo ───────────────────────────────────────────────────────
    if text.startswith("/start") or text.lower() in ("hola", "start"):
        tg_send(
            "👗 <b>MktFotos Bot</b>\n\n"
            "Mandame una foto de la prenda con el caption:\n\n"
            "• <b>Prany</b>\n"
            "• <b>Vaina</b>\n"
            "• <b>Ambas</b>\n\n"
            "Generaré las fotos, me decís si está bien o querés cambios, "
            "y cuando confirmás te pido el nombre para guardar en Drive 📁"
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

    # ── Fase: previewing — espera OK o corrección ──────────────────────────────
    if phase == "previewing":
        if words & OK_WORDS:
            # Usuario confirmó → pedir nombre
            set_phase("waiting_name")
            tg_send("✏️ ¿Cómo se llama el producto? (ese nombre se va a usar para la carpeta en Drive)")
            return
        else:
            # Corrección: regenerar con el texto como hint
            with SESSION_LOCK:
                brands        = list(SESSION["brands"])
                garment_bytes = SESSION["garment_bytes"]
                category      = SESSION["category"]
                flat_lay      = SESSION["flat_lay"]
                SESSION["correction"] = text

            tg_send(f"🔄 Aplicando corrección: <i>\"{text}\"</i>")
            threading.Thread(
                target=run_generation,
                args=(brands, garment_bytes, category, text, flat_lay),
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
        tg_send(
            "📸 Mandame una foto de la prenda con el caption de la marca:\n"
            "<b>Prany</b> / <b>Vaina</b> / <b>Ambas</b>"
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

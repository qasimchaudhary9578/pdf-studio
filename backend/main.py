import os
import shutil
import asyncio
import tempfile
import httpx
import fitz  # PyMuPDF
import pdfplumber
from openpyxl import Workbook
from pypdf import PdfReader, PdfWriter
from pdf2docx import Converter
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware

# Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://your-frontend.vercel.app")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
CHANNEL_URL = "https://t.me/yourunknownhelper"
CHANNEL_USERNAME = "@yourunknownhelper"

app = FastAPI(title="PDF Studio Hybrid Backend")

# Server Crash Protection (Max 2 heavy PDFs at once, 10MB max limit)
HEAVY_TASK_LOCK = asyncio.Semaphore(2)
MAX_FILE_SIZE = 10 * 1024 * 1024


async def limit_heavy_tasks():
    async with HEAVY_TASK_LOCK:
        yield


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def send_document_to_telegram(chat_id: str, file_path: str, filename: str, caption: str):
    """Processed file ko seedhe user ke Telegram chat mein bhejta hai."""
    url = f"{TELEGRAM_API}/sendDocument"
    async with httpx.AsyncClient(timeout=60.0) as client:
        with open(file_path, "rb") as f:
            files = {"document": (filename, f)}
            data = {"chat_id": chat_id, "caption": f"{caption}\n\n📢 Powered by {CHANNEL_USERNAME}"}
            resp = await client.post(url, data=data, files=files)
            if resp.status_code != 200:
                raise HTTPException(status_code=500, detail=f"Telegram delivery failed: {resp.text}")


@app.get("/")
def health_check():
    return {"status": "running", "service": "PDF Studio Bot & API"}


# 1. Telegram Bot Webhook (/start command handle karne ke liye)
@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    message = update.get("message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text.startswith("/start"):
        payload = {
            "chat_id": chat_id,
            "text": (
                "👋 *PDF & Image Studio mein swagat hai!*\n\n"
                "Neeche diye button par click karke saare tools use karein:\n"
                "• PDF & Image Compressor\n"
                "• PDF to Word & Excel\n"
                "• Images to PDF & PDF to Images\n"
                "• Lock & Unlock PDF"
            ),
            "parse_mode": "Markdown",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "🚀 Open PDF Studio", "web_app": {"url": MINI_APP_URL}}],
                    [{"text": "📢 Join Official Channel", "url": CHANNEL_URL}],
                ]
            },
        }
        async with httpx.AsyncClient() as client:
            await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)

    return {"ok": True}


# 2. Client-side processed file ko Telegram par bhejne ke liye
@app.post("/api/send-client-file")
async def send_client_file(
    chat_id: str = Form(...),
    caption: str = Form("Your processed file is ready!"),
    file: UploadFile = File(...),
):
    temp_dir = tempfile.mkdtemp()
    try:
        out_path = os.path.join(temp_dir, file.filename)
        with open(out_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        await send_document_to_telegram(chat_id, out_path, file.filename, caption)
        return {"status": "success", "message": "File sent to your Telegram chat!"}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# 3. PDF Compressor
@app.post("/api/compress-pdf", dependencies=[Depends(limit_heavy_tasks)])
async def compress_pdf(
    chat_id: str = Form(...),
    quality: int = Form(60),
    file: UploadFile = File(...),
):
    if file.size and file.size > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="Free version mein max 10MB tak ki PDF allow hai.")

    temp_dir = tempfile.mkdtemp()
    try:
        in_path = os.path.join(temp_dir, "input.pdf")
        out_name = f"compressed_{file.filename}"
        out_path = os.path.join(temp_dir, out_name)

        with open(in_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        doc = fitz.open(in_path)
        if doc.is_encrypted:
            doc.close()
            raise HTTPException(status_code=400, detail="Yeh PDF password-protected hai. Pehle ise Unlock karein.")

        orig_kb = round(os.path.getsize(in_path) / 1024, 1)
        q = max(15, min(quality, 90))
        target_dpi = 85 if q <= 35 else (115 if q <= 65 else 145)

        # Level 1: Direct image & font optimization
        try:
            doc.rewrite_images(
                dpi_threshold=72,
                dpi_target=target_dpi,
                quality=q,
                lossy=True,
            )
            doc.subset_fonts()
        except Exception:
            pass

        doc.save(out_path, garbage=4, deflate=True, clean=True)
        new_kb = round(os.path.getsize(out_path) / 1024, 1)

        # Level 2: Vector/certificate PDFs ke liye page optimization
        if new_kb >= orig_kb * 0.85:
            new_doc = fitz.open()
            for page in doc:
                pix = page.get_pixmap(dpi=target_dpi, alpha=False)
                img_bytes = pix.tobytes("jpeg", jpg_quality=q)
                new_page = new_doc.new_page(width=page.rect.width, height=page.rect.height)
                new_page.insert_image(new_page.rect, stream=img_bytes)
            new_doc.save(out_path, garbage=4, deflate=True)
            new_doc.close()
            new_kb = round(os.path.getsize(out_path) / 1024, 1)

        doc.close()

        final_path = out_path if new_kb < orig_kb else in_path
        final_kb = min(orig_kb, new_kb)

        caption = f"✅ Compressed PDF\nSize: {orig_kb} KB ➡️ {final_kb} KB"
        await send_document_to_telegram(chat_id, final_path, out_name, caption)
        return {"status": "success", "message": f"Compressed ({orig_kb}KB → {final_kb}KB) & sent to Telegram!"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Compression error: {str(e)}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# 4. Lock PDF (Password Lagana)
@app.post("/api/lock-pdf")
async def lock_pdf(
    chat_id: str = Form(...),
    password: str = Form(...),
    file: UploadFile = File(...),
):
    temp_dir = tempfile.mkdtemp()
    try:
        in_path = os.path.join(temp_dir, "input.pdf")
        out_name = f"protected_{file.filename}"
        out_path = os.path.join(temp_dir, out_name)

        with open(in_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        reader = PdfReader(in_path)
        if reader.is_encrypted:
            raise HTTPException(status_code=400, detail="Yeh PDF pehle se password-protected hai.")

        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.encrypt(password)

        with open(out_path, "wb") as f:
            writer.write(f)

        await send_document_to_telegram(chat_id, out_path, out_name, "🔒 Password Protected PDF")
        return {"status": "success", "message": "Password protected PDF sent to your Telegram chat!"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lock error: {str(e)}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# 5. Unlock PDF (Password Hatana)
@app.post("/api/unlock-pdf")
async def unlock_pdf(
    chat_id: str = Form(...),
    password: str = Form(...),
    file: UploadFile = File(...),
):
    temp_dir = tempfile.mkdtemp()
    try:
        in_path = os.path.join(temp_dir, "input.pdf")
        out_name = f"unlocked_{file.filename}"
        out_path = os.path.join(temp_dir, out_name)

        with open(in_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        reader = PdfReader(in_path)
        if reader.is_encrypted:
            res = reader.decrypt(password)
            if res == 0:
                raise HTTPException(status_code=400, detail="Galat password! Sahi password dalein.")

        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)

        with open(out_path, "wb") as f:
            writer.write(f)

        await send_document_to_telegram(chat_id, out_path, out_name, "🔓 Unlocked PDF")
        return {"status": "success", "message": "Unlocked PDF sent to your Telegram chat!"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unlock error: {str(e)}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# 6. PDF to Word (.docx)
@app.post("/api/pdf-to-word", dependencies=[Depends(limit_heavy_tasks)])
async def pdf_to_word(
    chat_id: str = Form(...),
    file: UploadFile = File(...),
):
    if file.size and file.size > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="Free version mein max 10MB tak ki PDF allow hai.")

    temp_dir = tempfile.mkdtemp()
    try:
        in_path = os.path.join(temp_dir, "input.pdf")
        base_name = os.path.splitext(file.filename)[0]
        out_name = f"{base_name}.docx"
        out_path = os.path.join(temp_dir, out_name)

        with open(in_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        cv = Converter(in_path)
        cv.convert(out_path)
        cv.close()

        await send_document_to_telegram(chat_id, out_path, out_name, "📝 Converted to Word (.docx)")
        return {"status": "success", "message": "Word file sent to your Telegram chat!"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Word conversion error: {str(e)}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# 7. PDF to Excel (.xlsx)
@app.post("/api/pdf-to-excel", dependencies=[Depends(limit_heavy_tasks)])
async def pdf_to_excel(
    chat_id: str = Form(...),
    file: UploadFile = File(...),
):
    if file.size and file.size > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="Free version mein max 10MB tak ki PDF allow hai.")

    temp_dir = tempfile.mkdtemp()
    try:
        in_path = os.path.join(temp_dir, "input.pdf")
        base_name = os.path.splitext(file.filename)[0]
        out_name = f"{base_name}.xlsx"
        out_path = os.path.join(temp_dir, out_name)

        with open(in_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        wb = Workbook()
        ws = wb.active
        ws.title = "Extracted Tables"
        has_data = False

        with pdfplumber.open(in_path) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        ws.append([cell if cell is not None else "" for cell in row])
                        has_data = True
                    ws.append([])

                if not tables:
                    text = page.extract_text()
                    if text:
                        for line in text.split("\n"):
                            ws.append([line])
                            has_data = True

        if not has_data:
            raise HTTPException(status_code=400, detail="Is PDF mein koi readable table ya text nahi mila.")

        wb.save(out_path)
        await send_document_to_telegram(chat_id, out_path, out_name, "📊 Converted to Excel (.xlsx)")
        return {"status": "success", "message": "Excel file sent to your Telegram chat!"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Excel conversion error: {str(e)}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
                                

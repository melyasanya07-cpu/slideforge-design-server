import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.parse import quote

from flask import Flask, jsonify, request, send_file
from PIL import Image, ImageStat


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "64")) * 1024 * 1024
CONVERSION_LOCK = threading.Semaphore(int(os.getenv("MAX_CONCURRENT_CONVERSIONS", "1")))
SUPPORTED_EXTENSIONS = {".pptx", ".potx"}
TEXT_TAG = re.compile(rb"(<a:t(?:\s[^>]*)?>)(.*?)(</a:t>)", re.DOTALL)


class ConversionError(RuntimeError):
    pass


def safe_stem(filename: str) -> str:
    stem = Path(filename or "design").stem.strip()
    stem = re.sub(r"[^\w\-. ]+", "_", stem, flags=re.UNICODE)
    return stem[:80] or "design"


def check_token() -> None:
    expected = os.getenv("CONVERTER_TOKEN", "").strip()
    if expected and request.headers.get("X-SlideForge-Token", "") != expected:
        raise PermissionError("Неверный токен сервера")


def strip_slide_text(source: Path, target: Path) -> None:
    """Create a PPTX copy with editable slide text removed, preserving graphics."""
    try:
        with zipfile.ZipFile(source, "r") as zin, zipfile.ZipFile(
            target, "w", compression=zipfile.ZIP_DEFLATED
        ) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", item.filename):
                    data = TEXT_TAG.sub(rb"\1\3", data)
                zout.writestr(item, data)
    except zipfile.BadZipFile as exc:
        raise ConversionError("Файл не является корректным PPTX/POTX") from exc


def run_checked(command: list[str], timeout: int = 180) -> None:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConversionError("Превышено время обработки презентации") from exc
    if result.returncode != 0:
        detail = (result.stdout or "").strip()[-1200:]
        raise ConversionError(f"Ошибка конвертера: {detail}")


def render_slides(presentation: Path, work: Path) -> list[Path]:
    pdf_dir = work / "pdf"
    image_dir = work / "layouts"
    profile_dir = work / "lo-profile"
    pdf_dir.mkdir()
    image_dir.mkdir()
    profile_dir.mkdir()

    profile_uri = "file://" + quote(str(profile_dir.resolve()))
    office_binary = os.getenv("LIBREOFFICE_BIN") or shutil.which("libreoffice") or shutil.which("soffice")
    if not office_binary:
        raise ConversionError("LibreOffice не установлен на сервере")
    run_checked([
        office_binary,
        f"-env:UserInstallation={profile_uri}",
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(pdf_dir),
        str(presentation),
    ])
    pdfs = list(pdf_dir.glob("*.pdf"))
    if not pdfs:
        raise ConversionError("LibreOffice не создал PDF")

    prefix = image_dir / "layout"
    run_checked([
        "pdftoppm",
        "-jpeg",
        "-r",
        os.getenv("RENDER_DPI", "120"),
        "-jpegopt",
        "quality=86,progressive=y,optimize=y",
        str(pdfs[0]),
        str(prefix),
    ])
    images = sorted(image_dir.glob("layout-*.jpg"))
    if not images:
        raise ConversionError("Не удалось получить изображения макетов")
    return images


def is_dark(image_path: Path) -> bool:
    with Image.open(image_path) as image:
        thumb = image.convert("RGB")
        thumb.thumbnail((64, 64))
        mean = ImageStat.Stat(thumb).mean
        luminance = 0.2126 * mean[0] + 0.7152 * mean[1] + 0.0722 * mean[2]
        return luminance < 118


def make_theme_package(name: str, images: list[Path]) -> io.BytesIO:
    output = io.BytesIO()
    manifest = {
        "format": "slideforge-theme-server-1",
        "name": name,
        "darkBackgrounds": is_dark(images[0]),
        "layoutCount": len(images),
        "layouts": [],
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, image_path in enumerate(images, start=1):
            archive_name = f"layouts/layout-{index:03d}.jpg"
            archive.write(image_path, archive_name)
            manifest["layouts"].append({"file": archive_name, "index": index})
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    output.seek(0)
    return output


@app.get("/")
def index():
    return jsonify({
        "service": "SlideForge Design Server",
        "status": "ok",
        "version": "1.0.0",
        "endpoints": {"health": "GET /health", "convert": "POST /convert"},
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "libreoffice": bool(shutil.which("libreoffice") or shutil.which("soffice")),
        "pdftoppm": bool(shutil.which("pdftoppm")),
    })


@app.post("/convert")
def convert():
    check_token()
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "Передайте PPTX/POTX в поле file"}), 400
    extension = Path(uploaded.filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        return jsonify({"error": "Поддерживаются только PPTX и POTX"}), 415

    name = request.form.get("name", "").strip() or safe_stem(uploaded.filename)
    with CONVERSION_LOCK:
        with tempfile.TemporaryDirectory(prefix="slideforge-") as temp:
            work = Path(temp)
            source = work / f"source{extension}"
            clean = work / "clean.pptx"
            uploaded.save(source)
            if source.stat().st_size < 100:
                return jsonify({"error": "Загруженный файл пуст"}), 400
            strip_slide_text(source, clean)
            images = render_slides(clean, work)
            package = make_theme_package(name, images)

    download_name = f"{safe_stem(name)}.slideforge-theme.zip"
    return send_file(
        package,
        mimetype="application/zip",
        as_attachment=True,
        download_name=download_name,
        max_age=0,
    )


@app.errorhandler(ConversionError)
def conversion_error(error):
    return jsonify({"error": str(error)}), 422


@app.errorhandler(PermissionError)
def permission_error(error):
    return jsonify({"error": str(error)}), 401


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "Файл превышает допустимый размер"}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

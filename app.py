import os
import re
import io
import json
import zipfile
import tempfile
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template
from playwright.sync_api import sync_playwright

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

# ── Slide detection ──────────────────────────────────────────────────────────

def detect_slides(html_content: str) -> list[dict]:
    """
    Analyse the HTML and return a list of slide descriptors:
      { index, selector, label }

    Strategy (ordered by specificity):
      1. Elements with class containing 'slide' (most common pattern)
      2. Direct children of body that are large block divs (fallback)
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_content, 'html.parser')

    # Priority 1 – explicit .slide class variants
    slides = soup.select('div[class*="slide"]')

    # Remove slides that are NESTED inside another slide (keep only top-level)
    def is_nested(tag, candidates):
        for parent in tag.parents:
            if parent in candidates and parent != tag:
                return True
        return False

    if slides:
        top_level = [s for s in slides if not is_nested(s, slides)]
        if top_level:
            result = []
            for i, s in enumerate(top_level):
                label = s.get('id') or f"slide-{i+1}"
                # try to grab a meaningful title from text
                title_el = s.find(['h1','h2','h3'])
                if title_el:
                    label = title_el.get_text(strip=True)[:40] or label
                result.append({'index': i, 'selector': None, 'label': label,
                                'nth': i, 'class_pattern': True})
            return result

    # Priority 2 – direct body children that look like full-page blocks
    body = soup.find('body')
    if body:
        children = [c for c in body.children
                    if hasattr(c, 'name') and c.name in ('div','section','article')]
        if len(children) >= 2:
            result = []
            for i, c in enumerate(children):
                label = c.get('id') or f"page-{i+1}"
                title_el = c.find(['h1','h2','h3'])
                if title_el:
                    label = title_el.get_text(strip=True)[:40] or label
                result.append({'index': i, 'selector': None, 'label': label,
                                'nth': i, 'class_pattern': False})
            return result

    # Fallback – treat entire page as 1 slide
    return [{'index': 0, 'selector': None, 'label': 'slide-1',
              'nth': 0, 'class_pattern': False}]


# ── Playwright rendering ──────────────────────────────────────────────────────

def render_slides_to_pngs(html_path: str, slides_meta: list[dict],
                           width: int, height: int) -> list[dict]:
    """
    Opens the HTML in a headless Chromium, measures each slide element,
    scrolls+clips to it, and captures a PNG.
    Returns list of { label, png_bytes }
    """
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=[
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--single-process',
        ])
        page = browser.new_page(viewport={'width': width, 'height': height})

        # Load file
        page.goto(f'file://{html_path}')
        # Wait for fonts / images
        page.wait_for_load_state('networkidle', timeout=15000)
        # Extra wait for web fonts
        page.wait_for_timeout(1500)

        # Figure out the selector to use for all slides
        has_slide_class = slides_meta[0].get('class_pattern', False) if slides_meta else False

        if has_slide_class:
            # Get all matching elements via JS
            js_selector = "div[class*='slide']"
            # Filter to top-level (not nested)
            elements_info = page.evaluate(f"""() => {{
                const all = Array.from(document.querySelectorAll("{js_selector}"));
                const topLevel = all.filter(el => !all.some(other => other !== el && other.contains(el)));
                return topLevel.map((el, i) => {{
                    const r = el.getBoundingClientRect();
                    const st = window.scrollY;
                    return {{
                        index: i,
                        x: r.left,
                        y: r.top + st,
                        width: r.width,
                        height: r.height
                    }};
                }});
            }}""")
        else:
            # Body direct children
            elements_info = page.evaluate("""() => {
                const body = document.body;
                const children = Array.from(body.children).filter(el =>
                    ['DIV','SECTION','ARTICLE'].includes(el.tagName));
                return children.map((el, i) => {
                    const r = el.getBoundingClientRect();
                    const st = window.scrollY;
                    return {
                        index: i,
                        x: r.left,
                        y: r.top + st,
                        width: r.width,
                        height: r.height
                    };
                });
            }""")

        if not elements_info:
            # Fallback: screenshot entire page
            png = page.screenshot(full_page=True, type='png')
            results.append({'label': 'slide-1', 'png_bytes': png})
            browser.close()
            return results

        # Take one full-page screenshot and crop each slide from it
        # This avoids viewport-resize artifacts and clip-out-of-bounds errors
        import PIL.Image as PILImage

        page.set_viewport_size({'width': width, 'height': height})
        page.wait_for_timeout(300)
        full_png = page.screenshot(type='png', full_page=True)

        full_img = PILImage.open(io.BytesIO(full_png))
        full_w, full_h = full_img.size

        for i, el in enumerate(elements_info):
            label = slides_meta[i]['label'] if i < len(slides_meta) else f'slide-{i+1}'
            safe_label = re.sub(r'[^a-zA-Z0-9_\-]', '_', label)[:50]
            filename = f"{i+1:02d}_{safe_label}"

            el_x = int(el['x'])
            el_y = int(el['y'])
            el_w = int(el['width'])  or width
            el_h = int(el['height']) or height

            # Clamp to actual image size
            x1 = max(0, el_x)
            y1 = max(0, el_y)
            x2 = min(full_w, el_x + el_w)
            y2 = min(full_h, el_y + el_h)

            if x2 <= x1 or y2 <= y1:
                continue  # skip empty clips

            cropped = full_img.crop((x1, y1, x2, y2))

            # Ensure exact target dimensions (pad if needed)
            if cropped.size != (width, height):
                canvas = PILImage.new('RGBA', (width, height), (0, 0, 0, 0))
                canvas.paste(cropped, (0, 0))
                cropped = canvas

            buf = io.BytesIO()
            cropped.save(buf, format='PNG', optimize=False)
            results.append({'label': filename, 'png_bytes': buf.getvalue()})

        browser.close()

    return results


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/analyse', methods=['POST'])
def analyse():
    """Quick analysis of uploaded HTML – returns slide count & labels."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    html = f.read().decode('utf-8', errors='replace')

    try:
        from bs4 import BeautifulSoup
        slides = detect_slides(html)
        return jsonify({
            'slide_count': len(slides),
            'slides': [{'index': s['index'], 'label': s['label']} for s in slides]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/convert', methods=['POST'])
def convert():
    """
    Accepts multipart form:
      file     – the HTML file
      width    – slide width  (default 1080)
      height   – slide height (default 1080)
    Returns a ZIP file with slide PNGs.
    """
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    f = request.files['file']
    width  = int(request.form.get('width',  1080))
    height = int(request.form.get('height', 1080))

    html_content = f.read().decode('utf-8', errors='replace')

    # Write HTML to temp file (playwright needs file://)
    with tempfile.NamedTemporaryFile(suffix='.html', delete=False, mode='w',
                                     encoding='utf-8') as tmp:
        tmp.write(html_content)
        tmp_path = tmp.name

    try:
        slides_meta = detect_slides(html_content)
        pngs = render_slides_to_pngs(tmp_path, slides_meta, width, height)

        # Build ZIP in memory
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for item in pngs:
                zf.writestr(f"{item['label']}.png", item['png_bytes'])
            # Tiny manifest
            manifest = json.dumps({
                'total_slides': len(pngs),
                'dimensions': f'{width}x{height}',
                'slides': [p['label'] for p in pngs]
            }, indent=2)
            zf.writestr('manifest.json', manifest)

        zip_buf.seek(0)
        base_name = Path(f.filename).stem or 'carousel'
        return send_file(
            zip_buf,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'{base_name}_slides.zip'
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        os.unlink(tmp_path)

# ── API endpoint ─────────────────────────────────────────────────────────────
@app.route('/api/convert', methods=['POST'])
def api_convert():
    """
    API endpoint — same as /convert but returns JSON error on failure.
    
    Usage:
      curl -X POST https://your-app.onrender.com/api/convert \
        -F "file=@carousel.html" \
        -F "width=1080" \
        -F "height=1080" \
        --output slides.zip
    """
    return convert()  # reuses existing logic


# ── Keep-alive ping (call this from cron) ────────────────────────────────────
@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({'status': 'ok', 'message': 'alive'}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

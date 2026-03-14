import os
import re
import uuid
from html import escape
from urllib.parse import quote
from flask import Flask, render_template, request, jsonify, Response
import pdfplumber
from werkzeug.utils import secure_filename

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'))
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024

KANJI_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\U00020000-\U0002a6df]')

# サーバーレス環境向けに遅延初期化
_tagger = None

def get_tagger():
    global _tagger
    if _tagger is None:
        import fugashi
        import unidic_lite
        _tagger = fugashi.Tagger(f'-d {unidic_lite.DICDIR}')
    return _tagger


def kata_to_hira(text):
    return ''.join(chr(ord(c) - 0x60) if '\u30a1' <= c <= '\u30f6' else c for c in text)


def get_reading(word):
    for attr in ('kana', 'pron'):
        try:
            r = getattr(word.feature, attr, None)
            if r and r != '*':
                return kata_to_hira(r)
        except Exception:
            pass
    return None


def get_word_readings(text, unique_only=False):
    """隣接する漢字形態素をひとつの複合語としてまとめる。
    例: 人手(ひとで) + 不足(ふそく) → 人手不足(ひとでふそく)
    """
    readings = []
    seen = set()
    pend_surf = ''
    pend_read = ''

    def flush():
        nonlocal pend_surf, pend_read
        if not pend_surf:
            return
        surf, read = pend_surf, pend_read
        pend_surf = pend_read = ''
        if not read:
            return
        if unique_only and surf in seen:
            return
        seen.add(surf)
        readings.append({'word': surf, 'furigana': read})

    for word in get_tagger()(text):
        surface = word.surface
        if KANJI_RE.search(surface):
            pend_surf += surface
            pend_read += get_reading(word) or ''
        else:
            flush()

    flush()
    return readings


def generate_table_html(readings, original_filename):
    rows = []
    for i, r in enumerate(readings, 1):
        word = escape(r.get('word', ''))
        furi = escape(r.get('furigana', ''))
        rows.append(
            f'<tr>'
            f'<td class="no">{i}</td>'
            f'<td class="word">{word}</td>'
            f'<td class="furi" contenteditable="true" spellcheck="false">{furi}</td>'
            f'</tr>'
        )
    rows_html = '\n'.join(rows)
    title = escape(original_filename)
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <title>振り仮名表 — {title}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Hiragino Sans','Yu Gothic',sans-serif; font-size: 11pt; color: #111; padding: 1.5rem 2rem; }}
    .toolbar {{ display: flex; align-items: center; gap: 1rem; margin-bottom: 1.2rem; padding-bottom: 0.9rem; border-bottom: 1px solid #ccc; }}
    .toolbar p {{ flex: 1; font-size: 0.82rem; color: #666; }}
    .print-btn {{ padding: 0.38rem 1.1rem; background: #4f6ef7; color: white; border: none; border-radius: 6px; font-size: 0.88rem; cursor: pointer; }}
    .print-btn:hover {{ background: #3a56d4; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 11pt; }}
    thead th {{ background: #f0f0f0; padding: 0.55rem 0.8rem; text-align: left; border: 1px solid #ccc; font-weight: bold; }}
    tbody td {{ padding: 0.45rem 0.8rem; border: 1px solid #ddd; vertical-align: middle; }}
    .no {{ width: 52px; color: #999; font-size: 0.85em; text-align: center; }}
    .word {{ font-size: 1.05em; font-weight: bold; width: 35%; }}
    .furi {{ color: #1a44c8; }}
    .furi[contenteditable]:hover {{ background: #f5f7ff; cursor: text; }}
    .furi[contenteditable]:focus {{ outline: 2px solid #4f6ef7; background: #fff; }}
    @media print {{
      .toolbar {{ display: none !important; }}
      body {{ padding: 0.5cm 1cm; }}
      td, th {{ border-color: #999 !important; }}
    }}
  </style>
</head>
<body>
  <div class="toolbar">
    <p><strong>振り仮名表：{title}</strong><br>読み仮名列をクリックして直接編集できます。</p>
    <button class="print-btn" onclick="window.print()">印刷 / PDF保存</button>
  </div>
  <table>
    <thead><tr><th class="no">No.</th><th class="word">単語</th><th class="furi">読み仮名（クリックで編集）</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  <script>
    document.querySelectorAll('.furi[contenteditable]').forEach(td => {{
      td.addEventListener('keydown', e => {{ if (e.key === 'Enter') {{ e.preventDefault(); td.blur(); }} }});
    }});
  </script>
</body>
</html>"""


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/analyze_region', methods=['POST'])
def analyze_region():
    """PDFファイルと選択範囲を受け取り、振り仮名を返す（ステートレス）"""
    if 'file' not in request.files:
        return jsonify({'error': 'ファイルが見つかりません'}), 400

    file = request.files['file']
    if not file or not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'PDFファイルを選択してください'}), 400

    page_num   = int(request.form.get('page', 1))
    x0         = float(request.form.get('x0', 0))
    top        = float(request.form.get('top', 0))
    x1         = float(request.form.get('x1', 0))
    bottom     = float(request.form.get('bottom', 0))
    unique_only = request.form.get('unique_only', 'false') == 'true'

    # /tmp に一時保存して処理（Vercel対応）
    filepath = f'/tmp/{uuid.uuid4()}.pdf'
    file.save(filepath)
    try:
        with pdfplumber.open(filepath) as pdf:
            if page_num < 1 or page_num > len(pdf.pages):
                return jsonify({'error': 'ページ番号が不正です'}), 400
            page = pdf.pages[page_num - 1]
            x0c = max(0, min(x0, page.width))
            x1c = max(0, min(x1, page.width))
            tc  = max(0, min(top,    page.height))
            bc  = max(0, min(bottom, page.height))
            if x1c <= x0c or bc <= tc:
                return jsonify({'error': '選択範囲が小さすぎます'}), 400
            text = page.crop((x0c, tc, x1c, bc)).extract_text() or ''
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)

    if not text.strip():
        return jsonify({'error': '選択範囲にテキストが見つかりませんでした（スキャン画像PDFは非対応）'}), 400

    readings = get_word_readings(text, unique_only=unique_only)
    return jsonify({'readings': readings, 'total': len(readings)})


@app.route('/download_table', methods=['POST'])
def download_table():
    data = request.json
    readings = data.get('readings', [])
    fname = data.get('filename', 'document.pdf')
    html = generate_table_html(readings, fname)
    stem = os.path.splitext(fname)[0]
    encoded = quote(f"{stem}_振り仮名表.html")
    return Response(
        html,
        mimetype='text/html; charset=utf-8',
        headers={'Content-Disposition': f"attachment; filename*=UTF-8''{encoded}"}
    )


if __name__ == '__main__':
    app.run(debug=True)

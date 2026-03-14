import os
import re
import uuid
from html import escape
from urllib.parse import quote
from flask import Flask, render_template, request, jsonify, Response
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextBox, LAParams
from werkzeug.utils import secure_filename

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'))
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024

KANJI_RE    = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\U00020000-\U0002a6df]')
KATAKANA_RE = re.compile(r'^[\u30a0-\u30ff]+$')

# サーバーレス環境向けに遅延初期化（pykakasi は純Python・軽量）
_kakasi = None

def get_kakasi():
    global _kakasi
    if _kakasi is None:
        import pykakasi
        _kakasi = pykakasi.kakasi()
    return _kakasi


def extract_region_text(filepath, page_num, x0, top, x1, bottom):
    """pdfminer.six で指定ページ・領域のテキストを抽出する。
    座標はPDF.jsと同じ top-left 基準（pdfminer は bottom-left なので変換する）。
    """
    laparams = LAParams(boxes_flow=0.5, word_margin=0.1)
    boxes = []
    seen_box_texts = set()

    with open(filepath, 'rb') as f:
        for layout in extract_pages(f, laparams=laparams, page_numbers=[page_num - 1]):
            h = layout.height
            # top-left 座標 → pdfminer bottom-left 座標へ変換
            pm_y0 = h - bottom   # 選択範囲の下端（ページ下から）
            pm_y1 = h - top      # 選択範囲の上端（ページ下から）

            for el in layout:
                if isinstance(el, LTTextBox):
                    ex0, ey0, ex1, ey1 = el.bbox
                    # 選択範囲と重なるテキストボックスを収集（同一テキストの重複除外）
                    if ex0 < x1 and ex1 > x0 and ey0 < pm_y1 and ey1 > pm_y0:
                        t = el.get_text().strip()
                        if t and t not in seen_box_texts:
                            seen_box_texts.add(t)
                            boxes.append((-ey1, ex0, t))

    boxes.sort()  # 上から下、左から右の順に並び替え
    raw = '\n'.join(t for _, _, t in boxes if t)
    # 日本語文字間の改行・空白を除去（pdfminer が単語を分割することへの対策）
    return re.sub(r'(?<=[^\x00-\x7F])\s+(?=[^\x00-\x7F])', '', raw)


def get_word_readings(text, unique_only=False):
    """漢字を含む単語を抽出する。
    ・直前のカタカナも単語に含める（例: チェーン+店 → チェーン店）
    ・pykakasi は複合語を1セグメントで返すので連結処理は不要
    """
    kks = get_kakasi()
    segments = list(kks.convert(text))
    readings = []
    seen = set()

    for i, seg in enumerate(segments):
        orig = seg['orig']
        hira = seg['hira']

        if not KANJI_RE.search(orig):
            continue

        # 直前がカタカナなら先頭に付加（チェーン店 など）
        # 振り仮名は漢字部分のみ（カタカナは自明なため不要）
        if i > 0 and KATAKANA_RE.match(segments[i - 1]['orig']):
            kata = segments[i - 1]['orig']
            word_surf = kata + orig
            word_read = hira   # 漢字部分の読みのみ
        else:
            word_surf = orig
            word_read = hira

        if not word_read:
            continue
        if unique_only and word_surf in seen:
            continue
        seen.add(word_surf)
        readings.append({'word': word_surf, 'furigana': word_read})

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
        text = extract_region_text(filepath, page_num, x0, top, x1, bottom)
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

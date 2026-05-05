import os
import re
import sqlite3
import html as html_lib
from urllib.parse import quote
from datetime import datetime, timedelta

import feedparser
import anthropic
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── SQLite DB 초기화 ─────────────────────────────────────────
DB_DIR  = os.path.join(os.path.dirname(__file__), 'data')
DB_PATH = os.path.join(DB_DIR, 'news_storage.db')

def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS news_table (
                id      TEXT PRIMARY KEY,
                title   TEXT,
                link    TEXT,
                snippet TEXT,
                source  TEXT,
                date    TEXT,
                lang    TEXT,
                cat     TEXT,
                kw      TEXT
            )
        ''')
        conn.commit()
    finally:
        conn.close()

init_db()


def save_to_db(items: list):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executemany(
            '''INSERT OR IGNORE INTO news_table
               (id, title, link, snippet, source, date, lang, cat, kw)
               VALUES (:id, :title, :link, :snippet, :source, :date, :lang, :cat, :kw)''',
            items
        )
        conn.commit()
    finally:
        conn.close()

# ── Google News RSS ─────────────────────────────────────────
LANG_PARAMS = {
    'ko': {'hl': 'ko',    'gl': 'KR', 'ceid': 'KR:ko'},
    'en': {'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'},
    'zh': {'hl': 'zh-CN', 'gl': 'CN', 'ceid': 'CN:zh-Hans'},
}
CAT_LABELS  = {'diplomacy': '외교', 'defense': '국방', 'economy': '경제'}
LANG_LABELS = {'ko': '한국어', 'en': 'English', 'zh': '中文'}

# ── konlpy (optional) ────────────────────────────────────────
_okt = None
HAS_KONLPY = False
try:
    from konlpy.tag import Okt
    _okt = Okt()
    HAS_KONLPY = True
except Exception:
    pass


def tokenize_ko(text: str) -> str:
    if HAS_KONLPY and _okt:
        try:
            return ' '.join(_okt.nouns(text))
        except Exception:
            pass
    words = re.split(r'[\s\-\xb7,\.!?()「」【】\[\]“”‘’:;\/\|…]+', text)
    return ' '.join(w for w in words if len(w) >= 2 and re.search(r'[가-힯]', w))


def prepare_doc(item: dict) -> str:
    text = (item.get('title', '') + ' ' + item.get('snippet', '')).strip()
    lang = item.get('lang', 'en')
    if lang == 'ko':
        return tokenize_ko(text)
    elif lang == 'zh':
        chunks = re.findall(r'[一-鿿]+', text)
        bigrams = [c[i:i+2] for c in chunks for i in range(len(c) - 1)]
        return ' '.join(bigrams) if bigrams else text
    else:
        return ' '.join(re.findall(r'[a-zA-Z]{3,}', text.lower()))


# ── Helpers ──────────────────────────────────────────────────
def build_rss_url(keyword: str, lang: str, date_from: str = '', date_to: str = '') -> str:
    p = LANG_PARAMS.get(lang, LANG_PARAMS['en'])
    q = keyword
    if date_from:
        q += f' after:{date_from}'
    if date_to:
        q += f' before:{date_to}'
    return (f"https://news.google.com/rss/search"
            f"?q={quote(q)}&hl={p['hl']}&gl={p['gl']}&ceid={p['ceid']}")


def clean_title(title: str) -> str:
    return re.sub(r'\s*-\s*[^-]+$', '', title).strip()


def strip_html(text: str) -> str:
    clean = re.sub(r'<[^>]+>', '', text)
    return html_lib.unescape(clean).strip()


# ── Routes ──────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


def _fetch_entries(keyword: str, lang: str, date_from: str, date_to: str) -> list:
    url = build_rss_url(keyword, lang, date_from, date_to)
    try:
        return feedparser.parse(url).entries
    except Exception:
        return []


def _split_date_ranges(date_from: str, date_to: str, max_days: int = 45) -> list:
    """Split a long date range into chunks of max_days each, newest first."""
    try:
        d_from = datetime.strptime(date_from, '%Y-%m-%d')
        d_to   = datetime.strptime(date_to,   '%Y-%m-%d')
    except ValueError:
        return [(date_from, date_to)]

    ranges = []
    chunk_end = d_to
    while chunk_end > d_from:
        chunk_start = max(d_from, chunk_end - timedelta(days=max_days))
        ranges.append((chunk_start.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')))
        chunk_end = chunk_start - timedelta(days=1)
    return ranges  # newest chunk first


@app.route('/api/fetch', methods=['POST'])
def fetch_news():
    data      = request.get_json(silent=True) or {}
    keyword   = data.get('keyword', '').strip()
    lang      = data.get('lang', 'ko')
    cat       = data.get('cat', 'diplomacy')
    date_from = data.get('date_from', '').strip()
    date_to   = data.get('date_to', '').strip()

    if not keyword:
        return jsonify({'error': '키워드가 없습니다'}), 400

    # Split long ranges to prevent Google News from dropping recent articles
    if date_from and date_to:
        try:
            span = (datetime.strptime(date_to, '%Y-%m-%d') -
                    datetime.strptime(date_from, '%Y-%m-%d')).days
        except ValueError:
            span = 0
        if span > 45:
            date_ranges = _split_date_ranges(date_from, date_to)
        else:
            date_ranges = [(date_from, date_to)]
    else:
        date_ranges = [(date_from, date_to)]

    raw_entries = []
    for df, dt in date_ranges:
        raw_entries.extend(_fetch_entries(keyword, lang, df, dt))

    items = []
    seen  = set()
    for entry in raw_entries:
        title = clean_title(entry.get('title', ''))
        if not title or title in seen:
            continue
        seen.add(title)

        link   = entry.get('link', '')
        source = ''
        if hasattr(entry, 'source') and isinstance(entry.source, dict):
            source = entry.source.get('title', '')
        elif hasattr(entry, 'source'):
            try:
                source = entry.source.title
            except AttributeError:
                pass

        summary   = strip_html(entry.get('summary', ''))[:200]
        published = entry.get('published', '')

        items.append({
            'id':      link or title,
            'title':   title,
            'link':    link,
            'snippet': summary,
            'source':  source,
            'date':    published,
            'lang':    lang,
            'cat':     cat,
            'kw':      keyword,
        })

    if items:
        save_to_db(items)

    return jsonify({'items': items, 'count': len(items)})


@app.route('/api/analyze', methods=['POST'])
def analyze():
    api_key = os.getenv('ANTHROPIC_API_KEY', '').strip()
    if not api_key:
        return jsonify({'error': '.env 파일에 ANTHROPIC_API_KEY가 없습니다'}), 500

    data       = request.get_json(silent=True) or {}
    news_items = data.get('news', [])
    if not news_items:
        return jsonify({'error': '분석할 뉴스가 없습니다'}), 400

    today = datetime.now().strftime('%Y년 %m월 %d일')
    dip   = sum(1 for n in news_items if n.get('cat') == 'diplomacy')
    def_  = sum(1 for n in news_items if n.get('cat') == 'defense')
    eco   = sum(1 for n in news_items if n.get('cat') == 'economy')

    lines = []
    for i, n in enumerate(news_items[:50]):
        cat_lbl  = CAT_LABELS.get(n.get('cat', ''), '?')
        lang_lbl = LANG_LABELS.get(n.get('lang', ''), '?')
        src = f" | {n['source']}" if n.get('source') else ''
        lines.append(f"[{i+1}] [{cat_lbl}][{lang_lbl}] {n.get('title','')}{src}")
    news_text = '\n'.join(lines)

    prompt = f"""당신은 동북아 외교·국방·경제 전문 분석가입니다.
오늘 날짜: {today}
수집된 뉴스: 외교 {dip}건, 국방 {def_}건, 경제 {eco}건 (총 {len(news_items)}건)

뉴스 헤드라인 목록:
{news_text}

위 뉴스를 분석하여 아래 형식으로 간결하고 전문적인 리포트를 작성해주세요:

## 📊 핵심 동향 요약
오늘의 주요 트렌드 3~5가지를 불렛으로 정리
주요 트렌드가 대한민국과 군사 안보에 미칠만한 영향 3~5가지를 블렛으로 정리
주요 트렌드가 대한민국과 교류협력 분야에 미칠만한 영향 3~5가지를 블렛으로 정리

## 🏛️ 외교 분야
주요 외교 이슈와 특이사항 분석

## ⚔️ 국방·안보 분야
군사·안보 동향 분석

## 💹 경제·통상 분야
경제 이슈 분석

## ⚠️ 주목 이슈 & 전망
특별히 주목할 사항 또는 향후 전망"""

    try:
        client  = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=4096,
            messages=[{'role': 'user', 'content': prompt}]
        )
        return jsonify({
            'text':  message.content[0].text,
            'model': message.model,
        })
    except anthropic.AuthenticationError:
        return jsonify({'error': 'API 키가 유효하지 않습니다. .env를 확인해주세요'}), 401
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/topics', methods=['POST'])
def get_topics():
    try:
        import numpy as np
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.cluster import KMeans
        from sklearn.metrics.pairwise import cosine_similarity
    except ImportError:
        return jsonify({'error': 'scikit-learn 미설치: pip install scikit-learn'}), 500

    data          = request.get_json(silent=True) or {}
    news_items    = data.get('news', [])
    n_topics      = max(2, min(15, int(data.get('n_topics', 7))))
    excl_raw      = data.get('excluded_words', [])
    excl_words    = [str(w).strip() for w in excl_raw if str(w).strip()]

    if not news_items:
        return jsonify({'error': '뉴스가 없습니다'}), 400
    if len(news_items) < n_topics:
        return jsonify({'error': f'기사 수({len(news_items)})가 토픽 수({n_topics})보다 적습니다'}), 400

    docs = [prepare_doc(item) for item in news_items]

    try:
        vec = TfidfVectorizer(
            max_features=500, min_df=1, sublinear_tf=True,
            stop_words=excl_words if excl_words else None,
        )
        X   = vec.fit_transform(docs)
        km  = KMeans(n_clusters=n_topics, random_state=42, n_init=10)
        labels = km.fit_predict(X)
    except Exception as e:
        return jsonify({'error': f'클러스터링 오류: {e}'}), 500

    # ── Perplexity: normalized inertia (lower = more compact clusters)
    perplexity = float(km.inertia_ / len(docs))

    # ── Coherence: avg cosine similarity of docs to their cluster centroid
    X_arr   = X.toarray()
    centers = km.cluster_centers_
    cohes   = []
    for i in range(n_topics):
        mask = labels == i
        if mask.sum() > 0:
            sims = cosine_similarity(X_arr[mask], centers[i:i+1]).flatten()
            cohes.append(float(sims.mean()))
    coherence = float(np.mean(cohes)) if cohes else 0.0

    # ── Elbow curve: run k=2..max_k to find optimal topic count
    max_k = min(15, len(docs) - 1)
    elbow_curve = []
    for k in range(2, max_k + 1):
        km_k = KMeans(n_clusters=k, random_state=42, n_init=5)
        km_k.fit(X)
        elbow_curve.append({'k': k, 'inertia': float(km_k.inertia_)})

    # ── Optimal k via second derivative of inertia (elbow point)
    optimal_k = n_topics
    if len(elbow_curve) >= 3:
        inertias   = [p['inertia'] for p in elbow_curve]
        diffs      = [inertias[i] - inertias[i+1] for i in range(len(inertias)-1)]
        diffs2     = [diffs[i] - diffs[i+1] for i in range(len(diffs)-1)]
        elbow_idx  = int(np.argmax(diffs2))
        optimal_k  = elbow_curve[elbow_idx + 1]['k']

    feature_names = vec.get_feature_names_out()
    topics = []
    for i in range(n_topics):
        center   = km.cluster_centers_[i]
        top_idx  = center.argsort()[-5:][::-1]
        keywords = [feature_names[j] for j in top_idx]
        items_i  = [news_items[j] for j, lbl in enumerate(labels) if lbl == i]
        topics.append({
            'id':       i,
            'keywords': keywords,
            'count':    len(items_i),
            'items':    items_i,
        })

    topics.sort(key=lambda t: t['count'], reverse=True)
    return jsonify({
        'topics':      topics,
        'has_konlpy':  HAS_KONLPY,
        'perplexity':  perplexity,
        'coherence':   coherence,
        'optimal_k':   optimal_k,
        'elbow_curve': elbow_curve,
    })


@app.route('/api/topics/name', methods=['POST'])
def name_topics():
    api_key = os.getenv('ANTHROPIC_API_KEY', '').strip()
    if not api_key:
        return jsonify({'error': 'ANTHROPIC_API_KEY 없음'}), 500

    data   = request.get_json(silent=True) or {}
    topics = data.get('topics', [])
    if not topics:
        return jsonify({'error': '토픽 없음'}), 400

    lines = [f"토픽 {i+1}: {', '.join(t.get('keywords', []))}"
             for i, t in enumerate(topics)]
    prompt = (
        "다음은 뉴스 클러스터 키워드입니다. 각 토픽에 간결한 이름(6자 이내 한국어)을 붙여주세요.\n\n"
        + "\n".join(lines)
        + "\n\n각 줄을 '토픽 N: [이름]' 형식으로만 답하세요."
    )

    try:
        client  = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=256,
            messages=[{'role': 'user', 'content': prompt}]
        )
        names = {}
        for line in message.content[0].text.strip().split('\n'):
            m = re.match(r'토픽\s*(\d+)\s*[:：]\s*(.+)', line.strip())
            if m:
                names[int(m.group(1)) - 1] = m.group(2).strip()
        return jsonify({'names': names})
    except anthropic.AuthenticationError:
        return jsonify({'error': 'API 키 오류'}), 401
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=5000, use_reloader=True)

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import urllib3
import re
import sqlite3
import os
from datetime import datetime, timedelta
import urllib.parse
import feedparser
import time
import difflib
from email.utils import parsedate_to_datetime

# 공공기관 및 언론사 사이트 SSL 인증서 경고 무시
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ★ CORS 허용 (Cloudflare Pages 프론트에서 API 호출 가능하게)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ★ 관리자 비밀번호 설정
ADMIN_PASSWORD = "admin"

# ★ 공공데이터포털에서 발급받은 인증키 (Decoding 키 사용)
KOSHA_API_KEY = "4b39abd89a4760da331813df65f3d422dbb86fca4ce6db701a0aa6919a49a9a4"

# ★ 카카오 검색 API 키
KAKAO_API_KEY = "c51f4f387a096e72053384f1884c20db"

# ★ 네이버 검색 API 키
NAVER_CLIENT_ID = "mYAQj4Z31nqvFt9mo1pb"
NAVER_CLIENT_SECRET = "UxVChm53gI"

# ★ DB 경로 (Render Persistent Disk 대응 / 로컬은 현재 폴더)
DB_DIR = os.environ.get("DB_DIR", ".")
QNA_DB = os.path.join(DB_DIR, "qna.db")
HISTORY_DB = os.path.join(DB_DIR, "history.db")


# =========================================================
# ★ 백엔드 보안 강화 (Security Headers)
# =========================================================
@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    return response


# --- [데이터베이스 초기화] ---
def init_db():
    # DB 폴더가 없으면 생성 (Render Disk 마운트 전 대응)
    if DB_DIR and not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR, exist_ok=True)

    # 1. Q&A 게시판용 DB
    conn = sqlite3.connect(QNA_DB)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS questions
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  author TEXT,
                  content TEXT,
                  created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS answers
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  question_id INTEGER,
                  author TEXT,
                  content TEXT,
                  created_at TEXT,
                  FOREIGN KEY(question_id) REFERENCES questions(id))''')
    conn.commit()
    conn.close()

    # 2. 방문자 조회수용 DB
    conn_hist = sqlite3.connect(HISTORY_DB)
    c_hist = conn_hist.cursor()
    c_hist.execute('''CREATE TABLE IF NOT EXISTS views
                      (date TEXT PRIMARY KEY, count INTEGER)''')
    conn_hist.commit()
    conn_hist.close()

init_db()

# --- [뉴스 캐시 전역 변수 (5분 갱신용)] ---
news_cache = {"data": [], "last_updated": None}
safety_cache = {"data": [], "last_updated": None}


# =========================================================
# 유틸: 날짜 검증 (1개월 이내 & 미래 날짜 차단)
# =========================================================
def is_valid_date(date_str):
    if not date_str:
        return False
    try:
        pub_dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
        now = datetime.now()
        one_month_ago = now - timedelta(days=30)
        if one_month_ago <= pub_dt <= now:
            return True
        return False
    except Exception:
        return False


# =========================================================
# 유틸: 고시·훈령·예규 제목에서 검색용 키워드만 깔끔히 추출
# =========================================================
def clean_admrule_title(title):
    if not title:
        return ""
    result = title
    for tok in ["(", "[", " - ", " – ", " — "]:
        idx = result.find(tok)
        if idx > 0:
            result = result[:idx]
    for n in range(1, 500):
        for suffix in ["조", "항", "관"]:
            for needle in ("제" + str(n) + suffix, "제 " + str(n) + suffix):
                idx = result.find(needle)
                if idx > 0:
                    result = result[:idx]
    result = result.strip()
    if not result:
        result = title.strip()
    return result


# =========================================================
# ★ 뉴스 수집 함수 (구글, 카카오)
# =========================================================
def fetch_google_news(keyword):
    news_list = []
    try:
        encoded_keyword = urllib.parse.quote(keyword)
        url = f"https://news.google.com/rss/search?q={encoded_keyword}&hl=ko&gl=KR&ceid=KR:ko"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(url, headers=headers, timeout=10)
        feed = feedparser.parse(response.content)

        for entry in feed.entries[:20]:
            pub_date_str = ""
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                dt = datetime.fromtimestamp(time.mktime(entry.published_parsed))
                pub_date_str = dt.strftime("%Y-%m-%d %H:%M")
            else:
                raw_date = entry.get('published', '')
                try:
                    dt = parsedate_to_datetime(raw_date)
                    pub_date_str = dt.strftime("%Y-%m-%d %H:%M")
                except:
                    pub_date_str = datetime.now().strftime("%Y-%m-%d %H:%M")

            if not is_valid_date(pub_date_str):
                continue

            title = entry.title.rsplit(" - ", 1)[0] if " - " in entry.title else entry.title
            news_list.append({
                "title": title,
                "link": entry.link,
                "published": pub_date_str,
                "source": "구글 뉴스"
            })
    except Exception as e:
        print(f"구글 뉴스 에러: {e}")
    return news_list

def fetch_kakao_news(keyword, api_key):
    if not api_key:
        return []
    news_list = []
    try:
        url = "https://dapi.kakao.com/v2/search/web"
        headers = {"Authorization": f"KakaoAK {api_key}"}
        params = {"query": keyword, "sort": "accuracy", "size": 30}
        response = requests.get(url, headers=headers, params=params, timeout=5)
        if response.status_code == 200:
            data = response.json()
            valid_keywords = ['안전', '재해', '산업', '중대', '노동', '사고', '보건', '위험', '산재', '중대재해']
            for item in data.get('documents', []):
                title = re.sub(r'<[^>]+>', '', item.get('title', ''))
                if not any(k in title for k in valid_keywords):
                    continue
                dt_str = item.get('datetime', '')
                pub_date = f"{dt_str[:10]} {dt_str[11:16]}" if dt_str else ""
                if not is_valid_date(pub_date):
                    continue
                news_list.append({
                    "title": title,
                    "link": item.get('url', ''),
                    "published": pub_date,
                    "source": "다음(카카오)"
                })
    except Exception as e:
        print(f"카카오 뉴스 에러: {e}")
    return news_list


# =========================================================
# ★ 안전전문지 전용 헤드라인 수집 함수
# =========================================================
def fetch_safety_headlines():
    news_list = []
    rss_urls = [
        ("안전신문", "https://www.safetynews.co.kr/rss/allArticle.xml"),
        ("세이프타임즈", "http://www.safetimes.co.kr/rss/allArticle.xml"),
        ("매일안전신문", "https://www.idsn.co.kr/rss/allArticle.xml")
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/rss+xml, application/xml, text/xml, */*"
    }
    for site_name, url in rss_urls:
        try:
            response = requests.get(url, headers=headers, timeout=10, verify=False)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            if not feed.entries:
                continue
            for entry in feed.entries[:3]:
                pub_date_str = ""
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    dt = datetime.fromtimestamp(time.mktime(entry.published_parsed))
                    pub_date_str = dt.strftime("%Y-%m-%d")
                else:
                    pub_date_str = datetime.now().strftime("%Y-%m-%d")
                news_list.append({
                    "title": entry.title,
                    "link": entry.link,
                    "published": pub_date_str,
                    "source": site_name
                })
        except Exception as e:
            print(f"[{site_name}] 헤드라인 수집 오류: {e}")
    return news_list


# =========================================================
# 라우트 (Routes)
# =========================================================
@app.route('/')
def index():
    # 프론트는 Cloudflare Pages가 담당. 여기는 헬스체크 용도.
    return jsonify({"status": "ok", "service": "safety-law-api"})

@app.route('/api/news')
def get_news():
    global news_cache
    if news_cache["data"] and news_cache["last_updated"]:
        if datetime.now() - news_cache["last_updated"] < timedelta(minutes=5):
            return jsonify({"status": "success", "data": news_cache["data"]})

    combined_news = []
    combined_news.extend(fetch_google_news("고용노동부 안전 OR 중대재해 OR 산업재해"))
    combined_news.extend(fetch_kakao_news("산업재해 중대재해", KAKAO_API_KEY))

    try:
        query = urllib.parse.quote("중대재해 사망")
        url = f"https://openapi.naver.com/v1/search/news.json?query={query}&display=50&sort=date"
        headers = {
            "X-Naver-Client-Id": NAVER_CLIENT_ID,
            "X-Naver-Client-Secret": NAVER_CLIENT_SECRET
        }
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            for item in data.get('items', []):
                title = re.sub(r'<[^>]+>', '', item.get('title', ''))
                pub_date = item.get('pubDate', '')
                try:
                    dt = parsedate_to_datetime(pub_date)
                    pub_date_str = dt.strftime("%Y-%m-%d %H:%M")
                except:
                    pub_date_str = pub_date[5:16]
                if not is_valid_date(pub_date_str):
                    continue
                combined_news.append({
                    "title": title,
                    "link": item.get('link', ''),
                    "published": pub_date_str,
                    "source": "네이버 뉴스"
                })
    except Exception as e:
        print(f"네이버 뉴스 에러: {e}")

    unique_news = []
    for news in combined_news:
        normalized_title = re.sub(r'\s+|\W+', '', news['title'])
        is_duplicate = False
        for existing_news in unique_news:
            existing_normalized = re.sub(r'\s+|\W+', '', existing_news['title'])
            similarity = difflib.SequenceMatcher(None, normalized_title, existing_normalized).ratio()
            if similarity >= 0.7:
                is_duplicate = True
                break
        if not is_duplicate:
            unique_news.append(news)

    unique_news.sort(key=lambda x: x['published'], reverse=True)
    unique_news = unique_news[:50]

    news_cache["data"] = unique_news
    news_cache["last_updated"] = datetime.now()
    return jsonify({"status": "success", "data": unique_news})

@app.route('/api/safety_headlines')
def api_safety_headlines():
    global safety_cache
    if safety_cache["data"] and safety_cache["last_updated"]:
        if datetime.now() - safety_cache["last_updated"] < timedelta(minutes=10):
            return jsonify({"status": "success", "data": safety_cache["data"]})
    data = fetch_safety_headlines()
    safety_cache["data"] = data
    safety_cache["last_updated"] = datetime.now()
    return jsonify({"status": "success", "data": data})

@app.route('/api/visit')
def get_views():
    today_str = datetime.now().strftime('%Y-%m-%d')
    conn = sqlite3.connect(HISTORY_DB)
    c = conn.cursor()
    c.execute("SELECT count FROM views WHERE date = ?", (today_str,))
    row = c.fetchone()
    if row:
        c.execute("UPDATE views SET count = count + 1 WHERE date = ?", (today_str,))
    else:
        c.execute("INSERT INTO views (date, count) VALUES (?, 1)", (today_str,))
    conn.commit()
    c.execute("SELECT count FROM views WHERE date = ?", (today_str,))
    today_count = c.fetchone()[0]
    c.execute("SELECT SUM(count) FROM views")
    total_count = c.fetchone()[0]
    conn.close()
    return jsonify({"status": "success", "today": today_count, "total": total_count})

# --- 안전보건공단 검색 API ---
@app.route('/api/search')
def api_search():
    keyword = request.args.get('q', '').strip()
    if not keyword:
        return jsonify({"status": "error", "message": "검색어를 입력하세요."})

    url = "https://apis.data.go.kr/B552468/srch/smartSearch"
    params = {
        "serviceKey": KOSHA_API_KEY,
        "pageNo": 1,
        "numOfRows": 100,
        "searchValue": keyword,
        "category": 0,
        "type": "json",
        "_type": "json"
    }
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        response = requests.get(url, params=params, headers=headers, verify=False, timeout=15)
        response.raise_for_status()

        if response.text.strip().startswith("<"):
            err_match = re.search(r"<returnAuthMsg>(.*?)</returnAuthMsg>", response.text)
            err_msg = err_match.group(1) if err_match else "API 인증 오류"
            return jsonify({"status": "error", "message": f"API 오류: {err_msg}"})

        data = response.json()
        body = data.get("response", {}).get("body", {})
        results = {"law": [], "guide": [], "media": []}

        cat_info = {
            "8":  {"name": "중대재해 처벌법",            "search_name": "중대재해 처벌 등에 관한 법률",       "priority": 1},
            "9":  {"name": "중대재해 처벌법 시행령",     "search_name": "중대재해 처벌 등에 관한 법률 시행령", "priority": 2},
            "1":  {"name": "산업안전보건법",             "search_name": "산업안전보건법",                      "priority": 3},
            "2":  {"name": "산업안전보건법 시행령",      "search_name": "산업안전보건법 시행령",               "priority": 4},
            "3":  {"name": "산업안전보건법 시행규칙",    "search_name": "산업안전보건법 시행규칙",             "priority": 5},
            "4":  {"name": "산업안전보건기준에 관한 규칙","search_name": "산업안전보건기준에 관한 규칙",        "priority": 6},
            "11": {"name": "유해·위험작업 취업제한 규칙","search_name": "유해ㆍ위험작업의 취업 제한에 관한 규칙","priority": 7},
            "5":  {"name": "고시·훈령·예규",             "search_name": "",                                    "priority": 8},
        }

        items = body.get("items", {})
        if isinstance(items, dict):
            item_list = items.get("item", [])
        else:
            item_list = []
        if isinstance(item_list, dict):
            item_list = [item_list]

        for item in item_list:
            cat_id = str(item.get("category", ""))
            title = re.sub(r"<[^>]+>", "", item.get("title", "") or "제목 없음")
            content = re.sub(r"<[^>]+>", "", item.get("highlight_content") or item.get("content") or "")

            if cat_id in cat_info:
                search_name = cat_info[cat_id].get("search_name", "")
                article_match = re.search(r'제(\d+)조(?:의(\d+))?', title)
                is_popup = False

                if search_name:
                    if article_match:
                        joNo = article_match.group(1)
                        joBrNo = article_match.group(2)
                        if joBrNo:
                            raw_url = f"https://www.law.go.kr/법령/{search_name}/제{joNo}조의{joBrNo}"
                        else:
                            raw_url = f"https://www.law.go.kr/법령/{search_name}/제{joNo}조"
                        law_link = urllib.parse.quote(raw_url, safe=":/")
                    else:
                        raw_url = f"https://www.law.go.kr/법령/{search_name}"
                        law_link = urllib.parse.quote(raw_url, safe=":/")
                else:
                    clean_title = clean_admrule_title(title)
                    law_link = (
                        "https://www.law.go.kr/admRulSc.do"
                        "?menuId=5&subMenuId=41&query="
                        + urllib.parse.quote(clean_title or title)
                    )

                # ★ 특정 키워드 검색 시 우선순위 강제 조정 및 내용 덮어쓰기
                custom_priority = cat_info[cat_id]["priority"]

                if keyword == "난간" and cat_info[cat_id]["name"] == "산업안전보건기준에 관한 규칙" and "제13조" in title:
                    custom_priority = -1

                if keyword == "매뉴얼" and cat_info[cat_id]["name"] == "중대재해 처벌법 시행령" and "제4조" in title:
                    custom_priority = -1
                    content = (
                        "8. 사업 또는 사업장에 중대산업재해가 발생하거나 발생할 급박한 위험이 있을 경우를 대비하여 "
                        "다음 각 목의 조치에 관한 매뉴얼을 마련하고, 해당 매뉴얼에 따라 조치하는지를 반기 1회 이상 점검할 것\n\n"
                        "가. 작업 중지, 근로자 대피, 위험요인 제거 등 대응조치\n"
                        "나. 중대산업재해를 입은 사람에 대한 구호조치\n"
                        "다. 추가 피해방지를 위한 조치"
                    )

                results["law"].append({
                    "category_name": cat_info[cat_id]["name"],
                    "title": title,
                    "content": content,
                    "priority": custom_priority,
                    "link": law_link,
                    "is_popup": is_popup
                })

            elif cat_id == "7":
                results["guide"].append({
                    "category_name": "KOSHA GUIDE",
                    "title": title,
                    "content": content,
                    "link": "https://portal.kosha.or.kr/archive/resources/tech-support/search/all?page=1&rowsPerPage=10",
                    "is_popup": False
                })

            elif cat_id == "6":
                results["media"].append({
                    "type": item.get("media_style", "미디어") or "미디어",
                    "title": title,
                    "link": item.get("filepath", "")
                })

        # 우선순위(priority)를 기준으로 오름차순 정렬
        results["law"] = sorted(results["law"], key=lambda x: x["priority"])

        for item in body.get("total_media", []) or []:
            filepath = item.get("filepath", "")
            if isinstance(filepath, list):
                filepath = filepath[0] if filepath else ""
            results["media"].append({
                "type": item.get("media_style", "미디어") or "미디어",
                "title": re.sub(r"<[^>]+>", "", item.get("title", "제목 없음")),
                "link": filepath
            })

        return jsonify({"status": "success", "data": results})

    except requests.exceptions.Timeout:
        return jsonify({"status": "error", "message": "API 응답 시간이 초과되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"데이터를 가져오는데 실패했습니다. ({str(e)})"})

@app.route('/api/qna', methods=['GET', 'POST'])
def handle_qna():
    conn = sqlite3.connect(QNA_DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if request.method == 'POST':
        data = request.json
        author = data.get('author', '익명').strip() or '익명'
        content = data.get('content', '').strip()
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        if not content:
            conn.close()
            return jsonify({"status": "error", "message": "내용을 입력하세요."})
        c.execute("INSERT INTO questions (author, content, created_at) VALUES (?, ?, ?)", (author, content, created_at))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    else:
        c.execute("SELECT * FROM questions ORDER BY id DESC")
        questions = [dict(row) for row in c.fetchall()]
        for q in questions:
            c.execute("SELECT * FROM answers WHERE question_id = ? ORDER BY id ASC", (q['id'],))
            q['answers'] = [dict(row) for row in c.fetchall()]
        conn.close()
        return jsonify({"status": "success", "data": questions})

@app.route('/api/qna/<int:q_id>/reply', methods=['POST'])
def handle_reply(q_id):
    data = request.json
    author = data.get('author', '익명').strip() or '익명'
    content = data.get('content', '').strip()
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not content:
        return jsonify({"status": "error", "message": "내용을 입력하세요."})
    conn = sqlite3.connect(QNA_DB)
    c = conn.cursor()
    c.execute("INSERT INTO answers (question_id, author, content, created_at) VALUES (?, ?, ?, ?)", (q_id, author, content, created_at))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/qna/question/<int:q_id>', methods=['DELETE'])
def delete_question(q_id):
    data = request.json
    if data.get('password') != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "관리자 비밀번호가 일치하지 않습니다."})
    conn = sqlite3.connect(QNA_DB)
    c = conn.cursor()
    c.execute("DELETE FROM answers WHERE question_id = ?", (q_id,))
    c.execute("DELETE FROM questions WHERE id = ?", (q_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/qna/answer/<int:a_id>', methods=['DELETE'])
def delete_answer(a_id):
    data = request.json
    if data.get('password') != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "관리자 비밀번호가 일치하지 않습니다."})
    conn = sqlite3.connect(QNA_DB)
    c = conn.cursor()
    c.execute("DELETE FROM answers WHERE id = ?", (a_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
